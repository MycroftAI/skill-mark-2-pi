# Copyright 2018 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import astral
import time
import arrow
from subprocess import call, check_output, CalledProcessError
from pytz import timezone
from datetime import datetime
from collections import namedtuple
from os.path import join

from mycroft.api import is_paired
from mycroft.messagebus.message import Message
from mycroft.skills.core import MycroftSkill
from mycroft.util.log import LOG
from mycroft.util.parse import normalize
from mycroft.util import play_wav
from mycroft import intent_file_handler

from PIL import Image, ImageDraw, ImageFont
import struct

# SJ201 capabilities
from .sj201_revA.switch import Switch
from .sj201_revA.led import Led
from .sj201_revA.volume import Volume
from mycroft.util import create_signal

# Basic drawing to the framebuffer
Color = namedtuple('Color', ['red', 'green', 'blue'])
Screen = namedtuple('Screen', ['height', 'width'])

SCREEN = Screen(800, 480)
BACKGROUND = Color(34, 167, 240)

FONT_PATH = 'NotoSansDisplay-Bold.ttf'

def fit_font(text, font_path, font_size):
    """ Brute force a good fontsize to make text fit screen. """
    font = ImageFont.truetype(font_path, font_size)
    w, h = font.getsize(text)
    while w < 0.9 * SCREEN.width:
        # iterate until the text size is just larger than the criteria
        font_size += 1
        font = ImageFont.truetype(font_path, font_size)
        w, h = font.getsize(text)

    return font


def write_fb(im, dev='/dev/fb0'):
    """ Write Image Object to framebuffer.

        TODO: Check memory mapping
    """
    start_time = time.time()
    cols = []
    for j in range(im.size[1] - 1):
        for i in range(im.size[0]):
            R, G, B, A = im.getpixel((i, j))
            # Write color data in the correct order for the screen
            cols.append(struct.pack('BBBB', B, G, R, A))
    LOG.info('Row time: {}'.format(time.time() - start_time))
    with open(dev, 'wb') as f:
        color = [BACKGROUND.blue, BACKGROUND.green, BACKGROUND.red, 0]
        f.write(struct.pack('BBBB', *color) *
                ((SCREEN.height - im.size[1]) // 2  * SCREEN.width))
        f.write(b''.join(cols))
        f.write(struct.pack('BBBB', *color) *
                ((SCREEN.height - im.size[1]) // 2  * SCREEN.width))

    LOG.debug('Draw time: {}'.format(time.time() - start_time))


def draw_file(file_path, dev='/dev/fb0'):
    """ Writes a file directly to the framebuff device.
    Arguments:
        file_path (str): path to file to be drawn to frame buffer device
        dev (str): Optional framebuffer device to write to
    """
    with open(file_path, 'rb') as img:
        with open(dev, 'wb') as fb:
            fb.write(img.read())


class Mark2(MycroftSkill):
    """
        The Mark2 skill handles much of the screen and audio activities
        related to Mycroft's core functionality.
    """
    def __init__(self):
        super().__init__('Mark2')

        self.settings['auto_brightness'] = False
        self.settings['use_listening_beep'] = True
        self.wifi_setup_executed = False

        # System volume
        self.volume = Volume()   # sj201 volume object
        self.muted = False
        self.show_init = True

        # Screen handling
        self.loading = True
        self.last_text = time.monotonic()
        self.skip_list = ('Mark2', 'TimeSkill.update_display')

        self.show_volume = False

        # SJ201 leds 
        self.led = Led()

        if self.show_init:
            # flash leds red, green, blue
            self.log.info("** Flashing Leds **")
            self.led.fill_leds( (255,0,0) )
            time.sleep(1)
            self.led.fill_leds( (0,255,0) )
            time.sleep(1)
            self.led.fill_leds( (0,0,255) )
            time.sleep(1)
            self.led.fill_leds( (0,0,0) )

        # SJ201 buttons and switch
        self.switches = Switch()
        self.switches.volume = self.volume
        self.switches.leds = self.led
        self.switches.user_action_handler = self.action_handler


    def action_handler(self):
        """ triggered when user presses the action button """
        create_signal('buttonPress')


    def initialize(self):
        """ Perform initalization.
            Registers messagebus handlers.
        """
        self.log.info("*** Enter Kivy Mark2 skill initialize ***")

        self.brightness_dict = self.translate_namedvalues('brightness.levels')

        try:
            # Handle Device Ready
            self.bus.on('mycroft.ready', self.reset_face)

            self.add_event('mycroft.speech.recognition.unknown',
                           self.handle_failed_stt)

            # Handle volume setting via I2C
            self.add_event('mycroft.volume.set', self.on_volume_set)
            self.add_event('mycroft.volume.get', self.on_volume_get)
            self.add_event('mycroft.volume.duck', self.on_volume_duck)
            self.add_event('mycroft.volume.unduck', self.on_volume_unduck)

        except Exception:
            LOG.exception('In Mark 2 Skill')

        # Update use of wake-up beep
        self._sync_wake_beep_setting()

        self.settings_change_callback = self.on_websettings_changed

        self.log.info("*** Exit Kivy Mark2 skill initialize ***")

    ###################################################################
    # System events
    def handle_show_text(self, message):
        self.log.debug("Drawing text to framebuffer")
        text = message.data.get('text')
        if text:
            text = text.strip()
            font = fit_font(text, self.find_resource(FONT_PATH, 'ui'), 30)
            w, h = font.getsize(text)
            image = Image.new('RGBA', (SCREEN.width, h), BACKGROUND)
            draw = ImageDraw.Draw(image)
            # Draw to center of screen
            draw.text(((SCREEN.width - w) / 2, 0), text,
                      fill='white', font=font)
            write_fb(image)

    ###################################################################
    # System volume

    def on_volume_set(self, message):
        """ Force vol between 0.0 and 1.0. """
        vol = message.data.get("percent", 0.5)
        vol = clip(vol, 0.0, 1.0)

        self.muted = False
        self.volume.set_hardware_volume(vol)
        self.show_volume = True

    def on_volume_get(self, message):
        """ Handle request for current volume. """
        self.bus.emit(message.response(data={'percent': self.volume.get_hardware_volume(),
                                             'muted': self.muted}))
        self.show_volume = message.data.get('show', False)

    def on_volume_duck(self, message):
        """ Handle ducking event by setting the output to 0. """
        self.muted = True
        self.mute_pulseaudio()
        self.volume.set_hardware_volume(0)

    def on_volume_unduck(self, message):
        """ Handle ducking event by setting the output to previous value. """
        self.muted = False
        self.unmute_pulseaudio()
        self.volume.set_hardware_volume(self.volume.volume)

    def mute_pulseaudio(self):
        """Mutes pulseaudio volume"""
        call(['pacmd', 'set-sink-mute', '0', 'true'])

    def unmute_pulseaudio(self):
        """Resets pulseaudio volume to max"""
        call(['pacmd', 'set-sink-mute', '0', 'false'])

    def reset_face(self, _):
        """Triggered after skills are initialized."""
        self.loading = False
        if is_paired():
            draw_file(self.find_resource('mycroft.fb', 'ui'))

    def shutdown(self):
        # Gotta clean up manually since not using add_event()
        pass

    def handle_failed_stt(self, message):
        """ No discernable words were transcribed. Show idle screen again. """
        pass

    #####################################################################
    # Web settings

    def on_websettings_changed(self):
        """ Update use of wake-up beep. """
        self._sync_wake_beep_setting()

    def _sync_wake_beep_setting(self):
        """ Update "use beep" global config from skill settings. """
        from mycroft.configuration.config import (
            LocalConf, USER_CONFIG, Configuration
        )
        config = Configuration.get()
        use_beep = self.settings.get('use_listening_beep') is True
        if not config['confirm_listening'] == use_beep:
            # Update local (user) configuration setting
            new_config = {
                'confirm_listening': use_beep
            }
            user_config = LocalConf(USER_CONFIG)
            user_config.merge(new_config)
            user_config.store()
            self.bus.emit(Message('configuration.updated'))

    #####################################################################
    # Brightness intent interaction

    def percent_to_level(self, percent):
        """ Converts the brigtness value from percentage to a
            value the Arduino can read

            Arguments:
                percent (int): interger value from 0 to 100

            return:
                (int): value form 0 to 30
        """
        return int(float(percent) / float(100) * 30)

    def parse_brightness(self, brightness):
        """ Parse text for brightness percentage.

            Arguments:
                brightness (str): string containing brightness level

            Returns:
                (int): brightness as percentage (0-100)
        """

        try:
            # Handle "full", etc.
            name = normalize(brightness)
            if name in self.brightness_dict:
                return self.brightness_dict[name]

            if '%' in brightness:
                brightness = brightness.replace("%", "").strip()
                return int(brightness)
            if 'percent' in brightness:
                brightness = brightness.replace("percent", "").strip()
                return int(brightness)

            i = int(brightness)
            if i < 0 or i > 100:
                return None

            if i < 30:
                # Assmume plain 0-30 is "level"
                return int((i * 100.0) / 30.0)

            # Assume plain 31-100 is "percentage"
            return i
        except Exception:
            return None  # failed in an int() conversion

    def set_screen_brightness(self, level, speak=True):
        """ Actually change screen brightness.

            Arguments:
                level (int): 0-30, brightness level
                speak (bool): when True, speak a confirmation
        """
        # TODO CHANGE THE BRIGHTNESS
        if speak is True:
            percent = int(float(level) * float(100) / float(30))
            self.speak_dialog(
                'brightness.set', data={'val': str(percent) + '%'})

    def _set_brightness(self, brightness):
        # brightness can be a number or word like "full", "half"
        percent = self.parse_brightness(brightness)
        if percent is None:
            self.speak_dialog('brightness.not.found.final')
        elif int(percent) is -1:
            self.handle_auto_brightness(None)
        else:
            self.auto_brightness = False
            self.set_screen_brightness(self.percent_to_level(percent))

    @intent_file_handler('brightness.intent')
    def handle_brightness(self, message):
        """ Intent handler to set custom screen brightness.

            Arguments:
                message (dict): messagebus message from intent parser
        """
        brightness = (message.data.get('brightness', None) or
                      self.get_response('brightness.not.found'))
        if brightness:
            self._set_brightness(brightness)

    def _get_auto_time(self):
        """ Get dawn, sunrise, noon, sunset, and dusk time.

            Returns:
                times (dict): dict with associated (datetime, level)
        """
        tz = self.location['timezone']['code']
        lat = self.location['coordinate']['latitude']
        lon = self.location['coordinate']['longitude']
        ast_loc = astral.Location()
        ast_loc.timezone = tz
        ast_loc.lattitude = lat
        ast_loc.longitude = lon

        user_set_tz = \
            timezone(tz).localize(datetime.now()).strftime('%Z')
        device_tz = time.tzname

        if user_set_tz in device_tz:
            sunrise = ast_loc.sun()['sunrise']
            noon = ast_loc.sun()['noon']
            sunset = ast_loc.sun()['sunset']
        else:
            secs = int(self.location['timezone']['offset']) / -1000
            sunrise = arrow.get(
                ast_loc.sun()['sunrise']).shift(
                    seconds=secs).replace(tzinfo='UTC').datetime
            noon = arrow.get(
                ast_loc.sun()['noon']).shift(
                    seconds=secs).replace(tzinfo='UTC').datetime
            sunset = arrow.get(
                ast_loc.sun()['sunset']).shift(
                    seconds=secs).replace(tzinfo='UTC').datetime

        return {
            'Sunrise': (sunrise, 20),  # high
            'Noon': (noon, 30),        # full
            'Sunset': (sunset, 5)      # dim
        }

    def schedule_brightness(self, time_of_day, pair):
        """ Schedule auto brightness with the event scheduler.

            Arguments:
                time_of_day (str): Sunrise, Noon, Sunset
                pair (tuple): (datetime, brightness)
        """
        d_time = pair[0]
        brightness = pair[1]
        now = arrow.now()
        arw_d_time = arrow.get(d_time)
        data = (time_of_day, brightness)
        if now.timestamp > arw_d_time.timestamp:
            d_time = arrow.get(d_time).shift(hours=+24)
            self.schedule_event(self._handle_screen_brightness_event, d_time,
                                data=data, name=time_of_day)
        else:
            self.schedule_event(self._handle_screen_brightness_event, d_time,
                                data=data, name=time_of_day)

    @intent_file_handler('brightness.auto.intent')
    def handle_auto_brightness(self, message):
        """ brightness varies depending on time of day

            Arguments:
                message (Message): messagebus message from intent parser
        """
        self.auto_brightness = True
        auto_time = self._get_auto_time()
        nearest_time_to_now = (float('inf'), None, None)
        for time_of_day, pair in auto_time.items():
            self.schedule_brightness(time_of_day, pair)
            now = arrow.now().timestamp
            t = arrow.get(pair[0]).timestamp
            if abs(now - t) < nearest_time_to_now[0]:
                nearest_time_to_now = (abs(now - t), pair[1], time_of_day)
        self.set_screen_brightness(nearest_time_to_now[1], speak=False)

    def _handle_screen_brightness_event(self, message):
        """ Wrapper for setting screen brightness from eventscheduler

            Arguments:
                message (Message): messagebus message
        """
        if self.auto_brightness is True:
            time_of_day = message.data[0]
            level = message.data[1]
            self.cancel_scheduled_event(time_of_day)
            self.set_screen_brightness(level, speak=False)
            pair = self._get_auto_time()[time_of_day]
            self.schedule_brightness(time_of_day, pair)


def create_skill():
    return Mark2()
