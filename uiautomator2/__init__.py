#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, annotations, print_function

import base64
import contextlib
import dataclasses
import io
import logging
import os
import re
import time
import warnings
from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from lxml import etree
from retry import retry

try:
    import adbutils
except ImportError:
    adbutils = None

try:
    from PIL import Image
except ImportError:
    Image = None

from uiautomator2 import xpath
from uiautomator2._input import InputMethodMixIn
from uiautomator2._proto import HTTP_TIMEOUT, SCROLL_STEPS, Direction
from uiautomator2._selector import Selector, UiObject
from uiautomator2.abstract import AbstractShell, AbstractUiautomatorServer, ShellResponse
from uiautomator2.base import _BaseClient
from uiautomator2.exceptions import *
from uiautomator2.settings import Settings
from uiautomator2.swipe import SwipeExt
from uiautomator2.utils import deprecated, image_convert, list2cmdline
from uiautomator2.watcher import WatchContext, Watcher

WAIT_FOR_DEVICE_TIMEOUT = int(os.getenv("WAIT_FOR_DEVICE_TIMEOUT", 20))

logger = logging.getLogger(__name__)
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

def enable_pretty_logging(level=logging.DEBUG):
    if not logger.handlers: # pragma: no cover
        # Configure handler
        handler = logging.StreamHandler()
        formatter = logging.Formatter('[%(levelname)1.1s %(asctime)s %(module)s:%(lineno)d pid:%(process)d] %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(level)

class _Device(_BaseClient):
    __orientation = (  # device orientation
        (0, "natural", "n", 0), (1, "left", "l", 90),
        (2, "upsidedown", "u", 180), (3, "right", "r", 270))

    def show_touch_trace(self, pointer_location: bool = True, show_touches: bool = True):
        """
        Show touch trace on device screen

        Args:
            pointer_location (bool): screen overlay showing current touch data
            show_touches (bool): show visual feedback for taps
        """
        self.shell(f"settings put system pointer_location {int(pointer_location)}")
        self.shell(f"settings put system show_touches {int(show_touches)}")

    def window_size(self):
        """ return (width, height) """
        w, h = self._dev.window_size()
        return w, h

    def screenshot(self, filename: Optional[str] = None, format="pillow", display_id: Optional[int] = None):
        """
        Take screenshot of device

        Returns:
            PIL.Image.Image, np.ndarray (OpenCV format) or None

        Args:
            filename (str): saved filename, if filename is set then return None
            format (str): used when filename is empty. one of ["pillow", "opencv"]
            display_id (int): use specific display if device has multiple screen

        Examples:
            screenshot("saved.jpg")
            screenshot().save("saved.png")
            cv2.imwrite('saved.jpg', screenshot(format='opencv'))
        """
        if display_id is None:
            base64_data = self.jsonrpc.takeScreenshot(1, 80)
            # takeScreenshot may return None
            if base64_data:
                jpg_raw = base64.b64decode(base64_data)
                pil_img = Image.open(io.BytesIO(jpg_raw))
            else:
                pil_img = self._dev.screenshot(display_id=0)
        else:
            pil_img = self._dev.screenshot(display_id=display_id)
        
        if filename:
            pil_img.save(filename)
            return
        return image_convert(pil_img, format)
        
    def dump_hierarchy(self, compressed=False, pretty=False, max_depth: Optional[int] = None) -> str:
        """
        Dump window hierarchy

        Args:
            compressed (bool): return compressed xml
            pretty (bool): pretty print xml
            max_depth (int): max depth of hierarchy

        Returns:
            xml content
        """
        try:
            if max_depth is None:
                max_depth = self.settings['max_depth']
            content = self._do_dump_hierarchy(compressed, max_depth)
        except HierarchyEmptyError: # pragma: no cover
            logger.warning("dump empty, return empty xml")
            content = '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\' ?>\r\n<hierarchy rotation="0" />'
        
        if pretty:
            root = etree.fromstring(content.encode("utf-8"))
            content = etree.tostring(root, pretty_print=True, encoding='UTF-8', xml_declaration=True)
            content = content.decode("utf-8")
        return content

    @retry(HierarchyEmptyError, tries=3, delay=1)
    def _do_dump_hierarchy(self, compressed=False, max_depth=None) -> str:
        if max_depth is None:
            max_depth = 50
        content = self.jsonrpc.dumpWindowHierarchy(compressed, max_depth)
        if content == "":
            raise HierarchyEmptyError("dump hierarchy is empty")
        
        # '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\' ?>\r\n<hierarchy rotation="0" />'
        if '<hierarchy rotation="0" />' in content:
            logger.debug("dump empty, call clear_traversed_text and retry")
            # self.clear_traversed_text()
            raise HierarchyEmptyError("dump hierarchy is empty with no children")
        return content

    def implicitly_wait(self, seconds: Optional[float] = None) -> float:
        """set default wait timeout
        Args:
            seconds(float): to wait element show up

        Returns:
            Current implicitly wait seconds

        Deprecated:
            recommend use: d.settings['wait_timeout'] = 10
        """
        if seconds:
            self.settings["wait_timeout"] = seconds
        return self.settings['wait_timeout']

    @property
    def pos_rel2abs(self):
        """
        returns a function which can convert percent size to pixel size
        """
        size = []

        def _convert(x, y):
            assert x >= 0
            assert y >= 0

            if (x < 1 or y < 1) and not size:
                size.extend(
                    self.window_size())  # size will be [width, height]

            if x < 1:
                x = int(size[0] * x)
            if y < 1:
                y = int(size[1] * y)
            return x, y

        return _convert

    @contextlib.contextmanager
    def _operation_delay(self, operation_name: str = None):
        before, after = self.settings['operation_delay']
        # 排除不要求延迟的方法
        if operation_name not in self.settings['operation_delay_methods']:
            before, after = 0, 0

        if before:
            logger.debug(f"operation [{operation_name}] pre-delay {before}s")
            time.sleep(before)
        yield
        if after:
            logger.debug(f"operation [{operation_name}] post-delay {after}s")
            time.sleep(after)

    @property
    def touch(self):
        """
        ACTION_DOWN: 0 ACTION_MOVE: 2
        touch.down(x, y)
        touch.move(x, y)
        touch.up(x, y)
        """
        ACTION_DOWN = 0
        ACTION_MOVE = 2
        ACTION_UP = 1

        obj: "Device" = self

        class _Touch(object):
            def down(self, x, y):
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_DOWN, x, y, 0)
                return self

            def move(self, x, y):
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_MOVE, x, y, 0)
                return self

            def up(self, x, y):
                """ ACTION_UP x, y """
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_UP, x, y, 0)
                return self

            def sleep(self, seconds: float):
                time.sleep(seconds)
                return self

        return _Touch()

    def click(self, x: Union[float, int], y: Union[float, int]):
        x, y = self.pos_rel2abs(x, y)
        with self._operation_delay("click"):
            self.jsonrpc.click(x, y)

    def double_click(self, x, y, duration=0.1):
        """
        double click position
        """
        x, y = self.pos_rel2abs(x, y)
        self.touch.down(x, y).up(x, y)
        time.sleep(duration)
        self.click(x, y)  # use click last is for htmlreport

    def long_click(self, x, y, duration: float = .5):
        '''long click at arbitrary coordinates.
        
        Args:
            duration (float): seconds of pressed
        '''
        x, y = self.pos_rel2abs(x, y)
        with self._operation_delay("click"):
            self.jsonrpc.click(x, y, int(duration*1000))

    def swipe(self, fx, fy, tx, ty, duration: Optional[float] = None, steps: Optional[int] = None):
        """
        Args:
            fx, fy: from position
            tx, ty: to position
            duration (float): duration
            steps: 1 steps is about 5ms, if set, duration will be ignore

        Documents:
            uiautomator use steps instead of duration
            As the document say: Each step execution is throttled to 5ms per step.

        Links:
            https://developer.android.com/reference/android/support/test/uiautomator/UiDevice.html#swipe%28int,%20int,%20int,%20int,%20int%29
        """
        if duration is not None and steps is not None:
            warnings.warn("duration and steps can not be set at the same time, use steps", UserWarning)
            duration = None
        if duration:
            steps = int(duration * 200)
        if not steps:
            steps = SCROLL_STEPS
        logger.debug("swipe from (%s, %s) to (%s, %s), steps: %d", fx, fy, tx, ty, steps)
        rel2abs = self.pos_rel2abs
        fx, fy = rel2abs(fx, fy)
        tx, ty = rel2abs(tx, ty)
        steps = max(2, steps)  # step=1 has no swipe effect
        with self._operation_delay("swipe"):
            return self.jsonrpc.swipe(fx, fy, tx, ty, steps)

    def swipe_points(self, points: List[Tuple[int, int]], duration: float = 0.5):
        """
        Args:
            points: is point array containg at least one point object. eg [[200, 300], [210, 320]]
            duration: duration to inject between two points

        Links:
            https://developer.android.com/reference/android/support/test/uiautomator/UiDevice.html#swipe(android.graphics.Point[], int)
        """
        ppoints = []
        rel2abs = self.pos_rel2abs
        for p in points:
            x, y = rel2abs(p[0], p[1])
            ppoints.append(x)
            ppoints.append(y)
        # Each step execution is throttled to 5ms per step. So for a 100 steps, the swipe will take about 1/ 2 second to complete
        steps = int(duration / .005)
        return self.jsonrpc.swipePoints(ppoints, steps)

    def drag(self, sx, sy, ex, ey, duration=0.5):
        '''Swipe from one point to another point.'''
        rel2abs = self.pos_rel2abs
        sx, sy = rel2abs(sx, sy)
        ex, ey = rel2abs(ex, ey)
        with self._operation_delay("drag"):
            return self.jsonrpc.drag(sx, sy, ex, ey, int(duration * 200))

    def press(self, key: Union[int, str], meta=None):
        """
        press key via name or key code. Supported key name includes:
            home, back, left, right, up, down, center, menu, search, enter,
            delete(or del), recent(recent apps), volume_up, volume_down,
            volume_mute, camera, power.
        """
        with self._operation_delay("press"):
            if isinstance(key, int):
                return self.jsonrpc.pressKeyCode(
                    key, meta) if meta else self.jsonrpc.pressKeyCode(key)
            else:
                return self.jsonrpc.pressKey(key)
    
    def long_press(self, key: Union[int, str]):
        """
        long press key via name or key code

        Args:
            key: key name or key code
        
        Examples:
            long_press("home") same as "adb shell input keyevent --longpress KEYCODE_HOME"
        """
        with self._operation_delay("press"):
            if isinstance(key, int):
                self.shell("input keyevent --longpress %d" % key)
            else:
                key = key.upper()
                self.shell(f"input keyevent --longpress {key}")

    def screen_on(self):
        self.jsonrpc.wakeUp()

    def screen_off(self):
        self.jsonrpc.sleep()

    @property
    def orientation(self) -> str:
        '''
        orienting the device to left/right or natural.
        left/l:       rotation=90 , displayRotation=1
        right/r:      rotation=270, displayRotation=3
        natural/n:    rotation=0  , displayRotation=0
        upsidedown/u: rotation=180, displayRotation=2
        '''
        return self.__orientation[self.info["displayRotation"]][1]

    @orientation.setter
    def orientation(self, value: str):
        '''setter of orientation property.'''
        for values in self.__orientation:
            if value in values:
                # can not set upside-down until api level 18.
                self.jsonrpc.setOrientation(values[1])
                break
        else:
            raise ValueError("Invalid orientation.")

    def freeze_rotation(self, freezed: bool = True):
        self.jsonrpc.freezeRotation(freezed)

    @property
    def last_traversed_text(self):
        '''get last traversed text. used in webview for highlighted text.'''
        return self.jsonrpc.getLastTraversedText()

    def clear_traversed_text(self):
        '''clear the last traversed text.'''
        self.jsonrpc.clearLastTraversedText()
    
    @property
    def last_toast(self) -> Optional[str]:
        return self.jsonrpc.getLastToast()
    
    def clear_toast(self):
        self.jsonrpc.clearLastToast()

    def open_notification(self):
        return self.jsonrpc.openNotification()

    def open_quick_settings(self):
        return self.jsonrpc.openQuickSettings()

    def open_url(self, url: str):
        self.shell(
            ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', url])

    def exists(self, **kwargs):
        return self(**kwargs).exists

    @property
    def clipboard(self) -> Optional[str]:
        return self.jsonrpc.getClipboard()

    @clipboard.setter
    def clipboard(self, text: str):
        self.set_clipboard(text)

    def set_clipboard(self, text, label=None):
        '''
        Args:
            text: The actual text in the clip.
            label: User-visible label for the clip data.
        '''
        self.jsonrpc.setClipboard(label, text)
    
    def clear_text(self):
        """ clear input text """
        self.jsonrpc.clearInputText()
    
    def send_keys(self, text: str):
        """
        send text to focused input area
        
        Args:
            text: input text
            clear: clear text before input
        """
        # 使用el =self(focused=True); el.set_text(el.get_text()+text)不可取
        # 因为placeholder中的文字也会加进去
        self.clipboard = text
        if self.clipboard != text:
            raise UiAutomationError("setClipboard failed")
        self.jsonrpc.pasteClipboard()

    def keyevent(self, v):
        """
        Args:
            v: eg home wakeup back
        """
        v = v.upper()
        self.shell("input keyevent " + v)

    @cached_property
    def serial(self) -> str:
        """
        If connected with USB, here should return self._serial
        When this situation happends

            d = u2.connect_usb("10.0.0.1:5555")
            d.serial # should be "10.0.0.1:5555"
            d.shell(['getprop', 'ro.serialno']).output.strip() # should uniq str like ffee123ca

        This logic should not change, because it used in tmq-service
        and if you break it, some people will not happy
        """
        if self._serial:
            return self._serial
        return self.shell(['getprop', 'ro.serialno']).output.strip()
    
    def __call__(self, **kwargs) -> 'UiObject':
        return UiObject(self, Selector(**kwargs))


class _AppMixIn(AbstractShell):
    def session(self, package_name: str, attach: bool = False) -> "Session":
        """
        launch app and keep watching the app's state

        Args:
            package_name: package name
            attach: attach to existing session or not

        Returns:
            Session
        """
        self.app_start(package_name, stop=not attach)
        return Session(self.adb_device, package_name)

    def _compat_shell_ps(self) -> str:
        """
        Compatible with some devices that does not support `ps` command
        """
        output = self.shell("ps -A").output
        if len(output.strip().splitlines()) <= 1:
            output = self.shell("ps").output
        return output.strip().replace("\r\n", "\n")
        
    def _pidof_app(self, package_name) -> Optional[int]:
        """
        Return pid of package name
        """
        output = self._compat_shell_ps()
        lines = output.splitlines()
        for line in lines:
            # line example: u0_a1    1318  123   1010000 27580 SyS_epoll_ 0000000000 S com.github.uiautomator
            fields = line.strip().split()
            if len(fields) < 9:
                continue
            if fields[-1] == package_name:
                return int(fields[1])

    def app_current(self):
        """
        Returns:
            dict(package, activity, pid?)

        Raises:
            DeviceError

        For developer:
            Function reset_uiautomator need this function, so can't use jsonrpc here.
        """
        info = self.adb_device.app_current()
        if info:
            return dataclasses.asdict(info)
        raise DeviceError("Couldn't get focused app")

    def app_install(self, data: str):
        """
        Install app

        Args:
            data: can be file path or url or file object
        """
        self.adb_device.install(data)

    def wait_activity(self, activity, timeout=10) -> bool:
        """ wait activity
        Args:
            activity (str): name of activity
            timeout (float): max wait time

        Returns:
            bool of activity
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_activity = self.app_current().get('activity')
            if activity == current_activity:
                return True
            time.sleep(.5)
        return False

    def app_start(self, package_name: str, activity: Optional[str] = None, wait: bool = False, stop: bool = False, use_monkey: bool = False):
        """ Launch application
        Args:
            package_name (str): package name
            activity (str): app activity
            stop (bool): Stop app before starting the activity. (require activity)
            use_monkey (bool): use monkey command to start app when activity is not given
            wait (bool): wait until app started. default False
        """
        if stop:
            self.app_stop(package_name)

        if use_monkey or not activity:
            self.shell([
                'monkey', '-p', package_name, '-c',
                'android.intent.category.LAUNCHER', '1'
            ])
            if wait:
                self.app_wait(package_name)
            return

        # if not activity:
        #     info = self.app_info(package_name)
        #     activity = info['mainActivity']
        #     if activity.find(".") == -1:
        #         activity = "." + activity

        # -D: enable debugging
        # -W: wait for launch to complete
        # -S: force stop the target app before starting the activity
        # --user <USER_ID> | current: Specify which user to run as; if not
        #    specified then run as the current user.
        # -e <EXTRA_KEY> <EXTRA_STRING_VALUE>
        # --ei <EXTRA_KEY> <EXTRA_INT_VALUE>
        # --ez <EXTRA_KEY> <EXTRA_BOOLEAN_VALUE>
        args = [
            'am', 'start', '-a', 'android.intent.action.MAIN', '-c',
            'android.intent.category.LAUNCHER',
            '-n', f'{package_name}/{activity}'
        ]
        self.shell(args)

        if wait:
            self.app_wait(package_name)

    def app_wait(self,
                 package_name: str,
                 timeout: float = 20.0,
                 front=False) -> int:
        """ Wait until app launched
        Args:
            package_name (str): package name
            timeout (float): maxium wait time
            front (bool): wait until app is current app

        Returns:
            pid (int) 0 if launch failed
        """
        pid = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if front:
                if self.app_current()['package'] == package_name:
                    pid = self._pidof_app(package_name)
            else:
                if package_name in self.app_list_running():
                    pid = self._pidof_app(package_name)
            if pid:
                return pid
            time.sleep(1)

        return pid or 0

    def app_list(self, filter: str = None) -> List[str]:
        """
        List installed app package names

        Args:
            filter: [-f] [-d] [-e] [-s] [-3] [-i] [-u] [--user USER_ID] [FILTER]
        
        Returns:
            list of apps by filter
        """
        output, _ = self.shell(['pm', 'list', 'packages', filter])
        packages = re.findall(r'package:([^\s]+)', output)
        return list(packages)

    def app_list_running(self) -> List[str]:
        """
        Returns:
            list of running apps
        """
        output, _ = self.shell('pm list packages')
        packages = re.findall(r'package:([^\s]+)', output)
        ps_output = self._compat_shell_ps()
        process_names = re.findall(r'(\S+)$', ps_output, re.M)
        return list(set(packages).intersection(process_names))

    def app_stop(self, package_name: str):
        """ Stop one application """
        self.adb_device.app_stop(package_name)

    def app_stop_all(self, excludes=[]):
        """ Stop all third party applications
        Args:
            excludes (list): apps that do now want to kill

        Returns:
            a list of killed apps
        """
        our_apps = ['com.github.uiautomator', 'com.github.uiautomator.test']
        kill_pkgs = set(self.app_list_running()).difference(our_apps +
                                                            excludes)
        for pkg_name in kill_pkgs:
            self.app_stop(pkg_name)
        return list(kill_pkgs)

    def app_clear(self, package_name: str):
        """ Stop and clear app data: pm clear """
        self.adb_device.app_clear(package_name)

    def app_uninstall(self, package_name: str) -> bool:
        """ Uninstall an app 

        Returns:
            bool: success
        """
        ret = self.shell(["pm", "uninstall", package_name])
        return ret.exit_code == 0

    def app_uninstall_all(self, excludes=[], verbose=False):
        """ Uninstall all apps """
        our_apps = ['com.github.uiautomator', 'com.github.uiautomator.test']
        output, _ = self.shell(['pm', 'list', 'packages', '-3'])
        pkgs = re.findall(r'package:([^\s]+)', output)
        pkgs = set(pkgs).difference(our_apps + excludes)
        pkgs = list(pkgs)
        for pkg_name in pkgs:
            if verbose:
                print("uninstalling", pkg_name, " ", end="", flush=True)
            ok = self.app_uninstall(pkg_name)
            if verbose:
                print("OK" if ok else "FAIL")

        return pkgs

    def app_info(self, package_name: str) -> Dict[str, Any]:
        """
        Get app info

        Args:
            package_name (str): package name

        Return example:
            {
                "versionName": "1.1.7",
                "versionCode": 1001007
            }

        Raises:
            AppNotFoundError
        """
        info = self.adb_device.app_info(package_name)
        if not info:
            raise AppNotFoundError("App not installed", package_name)
        return {
            "versionName": info.version_name,
            "versionCode": info.version_code,
        }

    def app_auto_grant_permissions(self, package_name: str):
        """ auto grant permissions

        Args:
            package_name (str): package name
        
        Help of "adb shell pm":
            grant [--user USER_ID] PACKAGE PERMISSION
            revoke [--user USER_ID] PACKAGE PERMISSION
                These commands either grant or revoke permissions to apps.  The permissions
                must be declared as used in the app's manifest, be runtime permissions
                (protection level dangerous), and the app targeting SDK greater than Lollipop MR1 (API level 22).
        
        Help of "Android official pm" see <https://developer.android.com/tools/adb#pm>
            Grant a permission to an app. On devices running Android 6.0 (API level 23) and higher,
              the permission can be any permission declared in the app manifest.
            On devices running Android 5.1 (API level 22) and lower,
              must be an optional permission defined by the app.
        """
        sdk_version_output = self.shell(['getprop', 'ro.build.version.sdk']).output.strip()
        sdk_version = int(sdk_version_output) if sdk_version_output.isdigit() else None
        if sdk_version is None:
            logger.warning("can't get sdk version")
            return
        if sdk_version < 23:
            # TODO: support android 5.1 (API 22) and lower
            logger.warning("auto grant permissions only support android 6.0+ (API 23+)")
            return
        
        dumpsys_package_output = self.shell(['dumpsys', 'package',  package_name]).output
        target_sdk_match = re.search(r'targetSdk=(\d+)', dumpsys_package_output)
        if not target_sdk_match:
            logger.warning("can't get targetSdk from dumpsys package")
            return
        target_sdk = int(target_sdk_match.group(1))
        if target_sdk < 22:
            logger.warning("auto grant permissions only support app targetSdk >= 22")
            return
            
        permissions = re.findall(r'(android\.\w*\.?permission\.\w+): granted=false', dumpsys_package_output)
        for permission in permissions:
            self.shell(['pm', 'grant', package_name, permission])
            logger.info(f'auto grant permission {permission}')


class _DeprecatedMixIn: # pragma: no cover
    @property
    def wait_timeout(self):  # wait element timeout
        return self.settings['wait_timeout']

    @wait_timeout.setter
    def wait_timeout(self, v: Union[int, float]):
        self.settings['wait_timeout'] = v

    @property
    def click_post_delay(self):
        """ Deprecated or not deprecated, this is a question """
        return self.settings['post_delay']

    @click_post_delay.setter
    def click_post_delay(self, v: Union[int, float]):
        self.settings['post_delay'] = v

    def unlock(self):
        """ unlock screen with swipe from left-bottom to right-top """
        if not self.info['screenOn']:
            # WAKEUP might be stuck
            self.shell("input keyevent POWER")
            self.swipe(0.1, 0.9, 0.9, 0.1)

    def show_float_window(self, show=True):
        """ 显示悬浮窗，提高uiautomator运行的稳定性 """
        print("show_float_window is deprecated, this is not needed anymore")
    
    @deprecated(reason="use d.toast.show(text, duration) instead")
    def make_toast(self, text, duration=1.0):
        """ Show toast
        Args:
            text (str): text to show
            duration (float): seconds of display
        """
        return self.jsonrpc.makeToast(text, duration * 1000)
    
    @property
    def toast(self):
        obj = self

        class Toast(object):
            def get_message(self,
                            wait_timeout=10,
                            cache_timeout=10,
                            default=None):
                """
                Args:
                    wait_timeout: seconds of max wait time if toast now show right now
                    cache_timeout: depreacated
                    default: default messsage to return when no toast show up

                Returns:
                    None or toast message
                """
                deadline = time.time() + wait_timeout
                while 1:
                    message = obj.jsonrpc.getLastToast()
                    if message:
                        return message
                    if time.time() > deadline:
                        return default
                    time.sleep(.5)

            def reset(self):
                return obj.jsonrpc.clearLastToast()

            def show(self, text, duration=1.0):
                return obj.jsonrpc.makeToast(text, duration * 1000)

        return Toast()
    
    def set_orientation(self, value: str):
        '''setter of orientation property.'''
        self.orientation = value


class _PluginMixIn:
    def watch_context(self, autostart: bool = True, builtin: bool = False) -> WatchContext:
        wc = WatchContext(self, builtin=builtin)
        if autostart:
            wc.start()
        return wc

    @cached_property
    def watcher(self) -> Watcher:
        return Watcher(self)

    @cached_property
    def xpath(self) -> xpath.XPathEntry:
        return xpath.XPathEntry(self)

    @cached_property
    def image(self):
        from uiautomator2 import image as _image
        return _image.ImageX(self)

    @cached_property
    def screenrecord(self):
        from uiautomator2 import screenrecord as _sr
        return _sr.Screenrecord(self)

    @cached_property
    def swipe_ext(self) -> SwipeExt:
        return SwipeExt(self)


class Device(_Device, _AppMixIn, _PluginMixIn, InputMethodMixIn, _DeprecatedMixIn):
    """ Device object """
    
    def clear_text(self):
        """ clear input text """
        if self.is_input_ime_installed():
            InputMethodMixIn.clear_text(self)
        else:
            _Device.clear_text(self)
    
    def send_keys(self, text: str, clear: bool = False):
        """
        send text to focused input area
        
        Args:
            text: input text
            clear: clear text before input
        """
        if clear:
            self.clear_text()    
        if self.is_input_ime_installed():
            InputMethodMixIn.send_keys(self, text)
            return
        try:
            _Device.send_keys(self, text)            
        except:
            # 安装输入法后继续输入
            InputMethodMixIn.send_keys(self, text)


class Session(Device):
    """Session keeps watch the app status
    each jsonrpc call will check if the package is still running
    """
    def __init__(self, dev: adbutils.AdbDevice, package_name: str):
        super().__init__(dev)
        self._package_name = package_name
        self._pid = self.app_wait(self._package_name)
    
    def running(self) -> bool:
        return self._pid == self._pidof_app(self._package_name)

    @property
    def pid(self) -> int:
        return self._pid
        
    def jsonrpc_call(self, method: str, params: Any = None, timeout: float = 10) -> Any:
        if not self.running():
            raise SessionBrokenError(f"app:{self._package_name} pid:{self._pid} is quit")
        return super().jsonrpc_call(method, params, timeout)
    
    def restart(self):
        """ restart app """
        self.app_start(self._package_name, wait=True, stop=True)
        self._pid = self._pidof_app(self._package_name)
    
    def close(self):
        """ close app """
        self.app_stop(self._package_name)
        self._pid = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def _is_http_url(value: Optional[str]) -> bool:
    return isinstance(value, str) and _HTTP_URL_RE.match(value) is not None


def _normalize_rpc_endpoint(url: str) -> str:
    endpoint = url.rstrip("/")
    if endpoint.endswith("/jsonrpc/0"):
        return endpoint
    if endpoint.endswith("/jsonrpc"):
        return endpoint + "/0"
    return endpoint + "/jsonrpc/0"


class HTTPDevice(_PluginMixIn, InputMethodMixIn, _DeprecatedMixIn):
    """Direct JSON-RPC client that talks to u2.jar HTTP server without ADB."""

    _ORIENTATION = (
        (0, "natural", "n", 0),
        (1, "left", "l", 90),
        (2, "upsidedown", "u", 180),
        (3, "right", "r", 270),
    )

    def __init__(self, rpc_url: str):
        self._debug = False
        self._rpc_endpoint = _normalize_rpc_endpoint(rpc_url)

    @property
    def debug(self) -> bool:
        return self._debug

    @debug.setter
    def debug(self, value: bool):
        self._debug = bool(value)

    @property
    def info(self) -> Dict[str, Any]:
        return self.jsonrpc.deviceInfo(http_timeout=10)

    @cached_property
    def settings(self) -> Settings:
        return Settings(self)

    def start_uiautomator(self):
        raise DeviceError("HTTPDevice cannot start uiautomator service remotely")

    def stop_uiautomator(self):
        raise DeviceError("HTTPDevice cannot stop uiautomator service remotely")

    def reset_uiautomator(self):
        raise DeviceError("HTTPDevice cannot reset uiautomator service remotely")

    def shell(self, cmdargs: Union[str, List[str]], timeout=60) -> ShellResponse:
        cmdline = list2cmdline(cmdargs)
        result = self._raw_jsonrpc_call(
            "executeShellCommand",
            (cmdline, int(timeout * 1000)),
            HTTP_TIMEOUT,
        )
        if isinstance(result, dict):
            stdout = result.get("stdout", "") or ""
            stderr = result.get("stderr", "") or ""
            code = int(result.get("returnCode", 0))
            return ShellResponse(stdout + stderr, code)
        return ShellResponse(str(result), 0)

    @property
    def adb_device(self) -> adbutils.AdbDevice:
        raise DeviceError("HTTPDevice has no adb device")

    def push(self, src, dst: str, mode=0o644):
        _ = (src, dst, mode)
        raise DeviceError("HTTPDevice.push is not supported")

    def pull(self, src: str, dst: str):
        _ = (src, dst)
        raise DeviceError("HTTPDevice.pull is not supported")

    def window_size(self) -> Tuple[int, int]:
        info = self.info
        return info["displayWidth"], info["displayHeight"]

    def show_touch_trace(self, pointer_location: bool = True, show_touches: bool = True):
        self.shell(f"settings put system pointer_location {int(pointer_location)}")
        self.shell(f"settings put system show_touches {int(show_touches)}")

    def implicitly_wait(self, seconds: Optional[float] = None) -> float:
        if seconds:
            self.settings["wait_timeout"] = seconds
        return self.settings["wait_timeout"]

    def sleep(self, seconds: float):
        time.sleep(seconds)

    @property
    def pos_rel2abs(self):
        size = []

        def _convert(x, y):
            assert x >= 0
            assert y >= 0

            if (x < 1 or y < 1) and not size:
                size.extend(self.window_size())

            if x < 1:
                x = int(size[0] * x)
            if y < 1:
                y = int(size[1] * y)
            return x, y

        return _convert

    @contextlib.contextmanager
    def _operation_delay(self, operation_name: str = None):
        before, after = self.settings["operation_delay"]
        if operation_name not in self.settings["operation_delay_methods"]:
            before, after = 0, 0

        if before:
            logger.debug("operation [%s] pre-delay %ss", operation_name, before)
            time.sleep(before)
        yield
        if after:
            logger.debug("operation [%s] post-delay %ss", operation_name, after)
            time.sleep(after)

    def _raw_jsonrpc_call(self, method: str, params: Any = None, timeout: float = 10) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        headers = {
            "User-Agent": "uiautomator2",
            "Accept-Encoding": "",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self._rpc_endpoint, json=payload, headers=headers, timeout=timeout
            )
        except requests.Timeout as e:
            raise HTTPTimeoutError(f"HTTP request timeout: {e}") from e
        except requests.RequestException as e:
            raise HTTPError(f"HTTP request failed: {e}") from e

        if response.status_code != 200:
            raise HTTPError(f"HTTP request failed: {response.status_code} {response.reason}")

        try:
            data = response.json()
        except ValueError as e:
            raise RPCInvalidError(f"Unknown RPC error: invalid json: {e}") from e

        if not isinstance(data, dict):
            raise RPCInvalidError("Unknown RPC error: not a dict")

        if "error" in data:
            code = data["error"].get("code")
            message = data["error"].get("message", "")
            stacktrace = data["error"].get("data")
            if "UiAutomation not connected" in response.text:
                raise UiAutomationNotConnectedError("UiAutomation not connected")
            if "android.os.DeadObjectException" in message:
                raise UiAutomationNotConnectedError("android.os.DeadObjectException")
            if "android.os.DeadSystemRuntimeException" in message:
                raise UiAutomationNotConnectedError("android.os.DeadSystemRuntimeException")
            if "uiautomator.UiObjectNotFoundException" in message:
                raise UiObjectNotFoundError(code, message, params)
            if "java.lang.StackOverflowError" in message:
                trimmed_stacktrace = (stacktrace or "")[:1000] + "..." + (stacktrace or "")[-1000:]
                raise RPCStackOverflowError(
                    f"StackOverflowError: {message}",
                    params,
                    trimmed_stacktrace,
                )
            raise RPCUnknownError(f"Unknown RPC error: {code} {message}", params, stacktrace)

        if "result" not in data:
            raise RPCInvalidError("Unknown RPC error: no result field")
        return data["result"]

    def jsonrpc_call(self, method: str, params: Any = None, timeout: float = 10) -> Any:
        return self._raw_jsonrpc_call(method, params, timeout)

    @property
    def jsonrpc(self):
        class JSONRpcWrapper():
            def __init__(self, server: "HTTPDevice"):
                self.server = server
                self.method = None

            def __getattr__(self, method):
                self.method = method
                return self

            def __call__(self, *args, **kwargs):
                http_timeout = kwargs.pop("http_timeout", HTTP_TIMEOUT)
                params = args if args else kwargs
                return self.server.jsonrpc_call(self.method, params, http_timeout)

        return JSONRpcWrapper(self)

    @retry(HierarchyEmptyError, tries=3, delay=1)
    def _do_dump_hierarchy(self, compressed=False, max_depth=None) -> str:
        if max_depth is None:
            max_depth = 50
        content = self.jsonrpc.dumpWindowHierarchy(compressed, max_depth)
        if content == "":
            raise HierarchyEmptyError("dump hierarchy is empty")
        if '<hierarchy rotation="0" />' in content:
            raise HierarchyEmptyError("dump hierarchy is empty with no children")
        return content

    def dump_hierarchy(self, compressed=False, pretty=False, max_depth: Optional[int] = None) -> str:
        try:
            if max_depth is None:
                max_depth = self.settings["max_depth"]
            content = self._do_dump_hierarchy(compressed, max_depth)
        except HierarchyEmptyError:  # pragma: no cover
            logger.warning("dump empty, return empty xml")
            content = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\r\n<hierarchy rotation=\"0\" />"

        if pretty:
            root = etree.fromstring(content.encode("utf-8"))
            content = etree.tostring(root, pretty_print=True, encoding="UTF-8", xml_declaration=True)
            content = content.decode("utf-8")
        return content

    def screenshot(self, filename: Optional[str] = None, format="pillow", display_id: Optional[int] = None):
        _ = display_id
        base64_data = self.jsonrpc.takeScreenshot(1, 80)
        if not base64_data:
            if self.settings["fallback_to_blank_screenshot"]:
                pil_img = Image.new("RGB", self.window_size(), (0, 0, 0))
            else:
                raise UiAutomationError("takeScreenshot failed")
        else:
            jpg_raw = base64.b64decode(base64_data)
            pil_img = Image.open(io.BytesIO(jpg_raw))

        if filename:
            pil_img.save(filename)
            return
        return image_convert(pil_img, format)

    def click(self, x: Union[float, int], y: Union[float, int]):
        x, y = self.pos_rel2abs(x, y)
        with self._operation_delay("click"):
            self.jsonrpc.click(x, y)

    def long_click(self, x, y, duration: float = .5):
        x, y = self.pos_rel2abs(x, y)
        with self._operation_delay("click"):
            self.jsonrpc.click(x, y, int(duration * 1000))

    def double_click(self, x, y, duration=0.1):
        x, y = self.pos_rel2abs(x, y)
        self.click(x, y)
        time.sleep(duration)
        self.click(x, y)

    def swipe(self, fx, fy, tx, ty, duration: Optional[float] = None, steps: Optional[int] = None):
        if duration is not None and steps is not None:
            warnings.warn("duration and steps can not be set at the same time, use steps", UserWarning)
            duration = None
        if duration:
            steps = int(duration * 200)
        if not steps:
            steps = SCROLL_STEPS
        rel2abs = self.pos_rel2abs
        fx, fy = rel2abs(fx, fy)
        tx, ty = rel2abs(tx, ty)
        steps = max(2, steps)
        with self._operation_delay("swipe"):
            return self.jsonrpc.swipe(fx, fy, tx, ty, steps)

    def swipe_points(self, points: List[Tuple[int, int]], duration: float = 0.5):
        ppoints = []
        rel2abs = self.pos_rel2abs
        for p in points:
            x, y = rel2abs(p[0], p[1])
            ppoints.append(x)
            ppoints.append(y)
        steps = int(duration / .005)
        return self.jsonrpc.swipePoints(ppoints, steps)

    def drag(self, sx, sy, ex, ey, duration=0.5):
        rel2abs = self.pos_rel2abs
        sx, sy = rel2abs(sx, sy)
        ex, ey = rel2abs(ex, ey)
        with self._operation_delay("drag"):
            return self.jsonrpc.drag(sx, sy, ex, ey, int(duration * 200))

    def press(self, key: Union[int, str], meta=None):
        with self._operation_delay("press"):
            if isinstance(key, int):
                return self.jsonrpc.pressKeyCode(key, meta) if meta else self.jsonrpc.pressKeyCode(key)
            return self.jsonrpc.pressKey(key)

    def long_press(self, key: Union[int, str]):
        with self._operation_delay("press"):
            if isinstance(key, int):
                self.shell(f"input keyevent --longpress {key}")
            else:
                self.shell(f"input keyevent --longpress {key.upper()}")

    def screen_on(self):
        self.jsonrpc.wakeUp()

    def screen_off(self):
        self.jsonrpc.sleep()

    @property
    def orientation(self) -> str:
        return self._ORIENTATION[self.info["displayRotation"]][1]

    @orientation.setter
    def orientation(self, value: str):
        for values in self._ORIENTATION:
            if value in values:
                self.jsonrpc.setOrientation(values[1])
                return
        raise ValueError("Invalid orientation.")

    def freeze_rotation(self, freezed: bool = True):
        self.jsonrpc.freezeRotation(freezed)

    def open_notification(self):
        return self.jsonrpc.openNotification()

    def open_quick_settings(self):
        return self.jsonrpc.openQuickSettings()

    def open_url(self, url: str):
        self.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])

    @property
    def clipboard(self) -> Optional[str]:
        return self.jsonrpc.getClipboard()

    @clipboard.setter
    def clipboard(self, text: str):
        self.set_clipboard(text)

    def set_clipboard(self, text, label=None):
        self.jsonrpc.setClipboard(label, text)

    def clear_text(self):
        self.jsonrpc.clearInputText()

    def send_keys(self, text: str, clear: bool = False):
        if clear:
            self.clear_text()
        self.clipboard = text
        if self.clipboard != text:
            raise UiAutomationError("setClipboard failed")
        self.jsonrpc.pasteClipboard()

    @property
    def last_traversed_text(self):
        return self.jsonrpc.getLastTraversedText()

    def clear_traversed_text(self):
        self.jsonrpc.clearLastTraversedText()

    @property
    def last_toast(self) -> Optional[str]:
        return self.jsonrpc.getLastToast()

    def clear_toast(self):
        self.jsonrpc.clearLastToast()

    def keyevent(self, v):
        self.shell("input keyevent " + str(v).upper())

    def _compat_shell_ps(self) -> str:
        output = self.shell("ps -A").output
        if len(output.strip().splitlines()) <= 1:
            output = self.shell("ps").output
        return output.strip().replace("\r\n", "\n")

    def _pidof_app(self, package_name) -> Optional[int]:
        output = self._compat_shell_ps()
        for line in output.splitlines():
            fields = line.strip().split()
            if len(fields) < 2:
                continue
            if fields[-1] == package_name and fields[1].isdigit():
                return int(fields[1])
        return None

    def app_current(self):
        package = (self.info or {}).get("currentPackageName")
        if not package:
            raise DeviceError("Couldn't get focused app")

        activity = None
        try:
            output = self.shell(["dumpsys", "window", "windows"]).output
            patterns = [
                r"mCurrentFocus=Window\{\S+\s+\S+\s+([^/\s]+)/([^\}\s]+)\}",
                r"mFocusedApp=.*\s([^/\s]+)/([^\}\s]+)\}",
            ]
            for pattern in patterns:
                m = re.search(pattern, output)
                if m and m.group(1) == package:
                    activity = m.group(2)
                    break
        except Exception:
            logger.debug("parse app_current activity failed", exc_info=True)

        result = {"package": package, "activity": activity}
        pid = self._pidof_app(package)
        if pid:
            result["pid"] = pid
        return result

    def wait_activity(self, activity, timeout=10) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_activity = self.app_current().get("activity")
            if activity == current_activity:
                return True
            time.sleep(.5)
        return False

    def app_start(
        self,
        package_name: str,
        activity: Optional[str] = None,
        wait: bool = False,
        stop: bool = False,
        use_monkey: bool = False,
    ):
        if stop:
            self.app_stop(package_name)

        if use_monkey or not activity:
            self.shell(
                [
                    "monkey",
                    "-p",
                    package_name,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ]
            )
            if wait:
                self.app_wait(package_name)
            return

        args = [
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-n",
            f"{package_name}/{activity}",
        ]
        self.shell(args)
        if wait:
            self.app_wait(package_name)

    def app_wait(self, package_name: str, timeout: float = 20.0, front=False) -> int:
        pid = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if front:
                if self.app_current()["package"] == package_name:
                    pid = self._pidof_app(package_name)
            else:
                if package_name in self.app_list_running():
                    pid = self._pidof_app(package_name)
            if pid:
                return pid
            time.sleep(1)
        return pid or 0

    def app_list(self, filter: str = None) -> List[str]:
        cmd = ["pm", "list", "packages"]
        if filter:
            cmd.append(filter)
        output = self.shell(cmd).output
        return re.findall(r"package:([^\s]+)", output)

    def app_list_running(self) -> List[str]:
        packages = set(self.app_list())
        ps_output = self._compat_shell_ps()
        process_names = re.findall(r"(\S+)$", ps_output, re.M)
        return list(packages.intersection(process_names))

    def app_stop(self, package_name: str):
        self.shell(["am", "force-stop", package_name])

    def app_stop_all(self, excludes=[]):
        our_apps = ["com.github.uiautomator", "com.github.uiautomator.test"]
        kill_pkgs = set(self.app_list_running()).difference(our_apps + excludes)
        for pkg_name in kill_pkgs:
            self.app_stop(pkg_name)
        return list(kill_pkgs)

    def app_clear(self, package_name: str):
        self.shell(["pm", "clear", package_name])

    def app_uninstall(self, package_name: str) -> bool:
        ret = self.shell(["pm", "uninstall", package_name])
        return ret.exit_code == 0

    def app_info(self, package_name: str) -> Dict[str, Any]:
        output = self.shell(["dumpsys", "package", package_name]).output
        if f"Package [{package_name}]" not in output and f"Package [{package_name} ]" not in output:
            raise AppNotFoundError("App not installed", package_name)

        version_name_match = re.search(r"versionName=([^\s]+)", output)
        version_code_match = re.search(r"versionCode=(\d+)", output)
        return {
            "versionName": version_name_match.group(1) if version_name_match else "",
            "versionCode": int(version_code_match.group(1)) if version_code_match else 0,
        }

    def app_uninstall_all(self, excludes=[], verbose=False):
        our_apps = ["com.github.uiautomator", "com.github.uiautomator.test"]
        output = self.shell(["pm", "list", "packages", "-3"]).output
        pkgs = re.findall(r"package:([^\s]+)", output)
        pkgs = set(pkgs).difference(our_apps + excludes)
        pkgs = list(pkgs)
        for pkg_name in pkgs:
            if verbose:
                print("uninstalling", pkg_name, " ", end="", flush=True)
            ok = self.app_uninstall(pkg_name)
            if verbose:
                print("OK" if ok else "FAIL")
        return pkgs

    def app_auto_grant_permissions(self, package_name: str):
        sdk_version_output = self.shell(["getprop", "ro.build.version.sdk"]).output.strip()
        sdk_version = int(sdk_version_output) if sdk_version_output.isdigit() else None
        if sdk_version is None:
            logger.warning("can't get sdk version")
            return
        if sdk_version < 23:
            logger.warning("auto grant permissions only support android 6.0+ (API 23+)")
            return

        dumpsys_package_output = self.shell(["dumpsys", "package", package_name]).output
        target_sdk_match = re.search(r"targetSdk=(\d+)", dumpsys_package_output)
        if not target_sdk_match:
            logger.warning("can't get targetSdk from dumpsys package")
            return
        target_sdk = int(target_sdk_match.group(1))
        if target_sdk < 22:
            logger.warning("auto grant permissions only support app targetSdk >= 22")
            return

        permissions = re.findall(
            r"(android\.\w*\.?permission\.\w+): granted=false",
            dumpsys_package_output,
        )
        for permission in permissions:
            self.shell(["pm", "grant", package_name, permission])
            logger.info("auto grant permission %s", permission)

    def app_install(self, data: str):
        if not isinstance(data, str):
            raise DeviceError("HTTPDevice.app_install only supports str path/url")

        data = data.strip()
        if not data:
            raise DeviceError("empty install path/url")

        if data.startswith("http://") or data.startswith("https://"):
            target = "/data/local/tmp/u2_http_install.apk"
            download_cmd = (
                f"(curl -L -o {target} {data} || "
                f"toybox wget -O {target} {data} || "
                f"wget -O {target} {data})"
            )
            ret = self.shell(download_cmd, timeout=300)
            if ret.exit_code != 0:
                raise DeviceError(f"download apk failed: {ret.output}")
            install_ret = self.shell(["pm", "install", "-r", target], timeout=300)
            if install_ret.exit_code != 0:
                raise DeviceError(f"install apk failed: {install_ret.output}")
            return

        if os.path.isabs(data):
            if os.path.exists(data):
                raise DeviceError("local apk file install is not supported over HTTP, use device path or URL")
            install_ret = self.shell(["pm", "install", "-r", data], timeout=300)
            if install_ret.exit_code != 0:
                raise DeviceError(f"install apk failed: {install_ret.output}")
            return

        raise DeviceError("unsupported install path, use device absolute path or URL")

    def session(self, package_name: str, attach: bool = False) -> "HTTPSession":
        self.app_start(package_name, stop=not attach)
        pid = self.app_wait(package_name)
        return HTTPSession(self._rpc_endpoint, package_name, pid=pid)

    def exists(self, **kwargs):
        return self(**kwargs).exists

    @property
    def touch(self):
        ACTION_DOWN = 0
        ACTION_MOVE = 2
        ACTION_UP = 1
        obj = self

        class _Touch(object):
            def down(self, x, y):
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_DOWN, x, y, 0)
                return self

            def move(self, x, y):
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_MOVE, x, y, 0)
                return self

            def up(self, x, y):
                x, y = obj.pos_rel2abs(x, y)
                obj.jsonrpc.injectInputEvent(ACTION_UP, x, y, 0)
                return self

            def sleep(self, seconds: float):
                time.sleep(seconds)
                return self

        return _Touch()

    @cached_property
    def serial(self) -> str:
        return self._rpc_endpoint

    @cached_property
    def swipe_ext(self) -> SwipeExt:
        return SwipeExt(self)

    @cached_property
    def watcher(self) -> Watcher:
        return Watcher(self)

    @cached_property
    def xpath(self) -> xpath.XPathEntry:
        return xpath.XPathEntry(self)

    def __call__(self, **kwargs) -> "UiObject":
        return UiObject(self, Selector(**kwargs))


class HTTPSession(HTTPDevice):
    """Session for HTTPDevice, validates target app process before each JSON-RPC call."""

    def __init__(self, rpc_url: str, package_name: str, pid: Optional[int] = None):
        super().__init__(rpc_url)
        self._package_name = package_name
        self._pid = pid if pid is not None else self.app_wait(self._package_name)

    def running(self) -> bool:
        return self._pid == self._pidof_app(self._package_name)

    @property
    def pid(self) -> int:
        return self._pid

    def jsonrpc_call(self, method: str, params: Any = None, timeout: float = 10) -> Any:
        if method == "executeShellCommand":
            return self._raw_jsonrpc_call(method, params, timeout)
        if not self.running():
            raise SessionBrokenError(f"app:{self._package_name} pid:{self._pid} is quit")
        return super().jsonrpc_call(method, params, timeout)

    def restart(self):
        self.app_start(self._package_name, wait=True, stop=True)
        self._pid = self._pidof_app(self._package_name)

    def close(self):
        self.app_stop(self._package_name)
        self._pid = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def connect_http(rpc_url: str) -> HTTPDevice:
    """Connect directly to a u2.jar JSON-RPC endpoint over HTTP."""
    return HTTPDevice(rpc_url)


def connect(serial: Union[str, adbutils.AdbDevice] = None) -> Union[Device, HTTPDevice]:
    """
    Args:
        serial (str): Android device serialno

    Returns:
        Device

    Raises:
        ConnectError

    Example:
        connect("10.0.0.1:5555")
        connect("cff1123ea")  # adb device serial number
        connect("http://192.168.50.27:9008")
    """
    if _is_http_url(serial):
        return connect_http(serial)

    if not serial:
        rpc_url = os.getenv("UIAUTOMATOR2_RPC_URL")
        if rpc_url:
            return connect_http(rpc_url)
        serial = os.getenv("ANDROID_SERIAL")
        if _is_http_url(serial):
            return connect_http(serial)
    return connect_usb(serial)


def connect_usb(serial: Optional[str] = None) -> Device:
    """
    Args:
        serial (str): android device serial

    Returns:
        Device

    Raises:
        ConnectError
    """
    if not serial:
        serial = adbutils.adb.device()
    return Device(serial)
