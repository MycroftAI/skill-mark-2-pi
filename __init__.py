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
import subprocess
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
from pixel_ring import pixel_ring
import struct

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
        self.settings['use_listening_beep'] = False

        # System volume
        self.volume = 0.5
        self.muted = False
        self.get_hardware_volume()       # read from the device

        # Screen handling
        self.loading = True
        self.showing = False
        self.last_text = time.monotonic()
        self.skip_list = ('Mark2', 'TimeSkill.update_display')

        # LEDs
        pixel_ring.set_vad_led(False)  # No red center LED speech indication
        self.main_blue = 0x22A7F0
        self.tertiary_blue = 0x4DE0FF
        self.tertiary_green = 0x40DBB0
        self.num_leds = 12
        self.show_volume = False
        self.speaking = False

    def initialize(self):
        """ Perform initalization.

            Registers messagebus handlers.
        """
        self.brightness_dict = self.translate_namedvalues('brightness.levels')


        try:
            # Handle Wi-Fi Setup visuals
            self.add_event('system.wifi.ap_up',
                            self.handle_ap_up)
            self.add_event('system.wifi.ap_device_connected',
                           self.handle_wifi_device_connected)
            self.add_event('system.wifi.ap_device_disconnected',
                            self.handle_ap_up)
            self.add_event('system.wifi.ap_connection_success',
                            self.handle_ap_success)

            # Handle Pairing Visuals
            self.add_event('mycroft.paired',
                           self.handle_paired)
            self.add_event('mycroft.internet.connected',
                           self.handle_internet_connected)

            # Handle the 'waking' visual
            self.add_event('recognizer_loop:record_begin',
                           self.handle_listener_started)
            self.add_event('recognizer_loop:record_end',
                           self.handle_listener_ended)
            self.add_event('mycroft.speech.recognition.unknown',
                           self.handle_failed_stt)

            # Handle the 'busy' visual
            self.bus.on('mycroft.skill.handler.start',
                        self.on_handler_started)
            self.bus.on('mycroft.skill.handler.complete',
                        self.on_handler_complete)

            # Handle the 'speaking' visual
            self.bus.on('recognizer_loop:audio_output_start',
                        self.on_handler_audio_start)
            self.bus.on('recognizer_loop:audio_output_end',
                        self.on_handler_audio_end)

            self.bus.on('mycroft.ready', self.reset_face)

            # System events
            self.add_event('system.reboot', self.handle_system_reboot)
            self.add_event('system.shutdown', self.handle_system_shutdown)

            # Handle volume setting via I2C
            self.add_event('mycroft.volume.set', self.on_volume_set)
            self.add_event('mycroft.volume.get', self.on_volume_get)
            self.add_event('mycroft.volume.duck', self.on_volume_duck)
            self.add_event('mycroft.volume.unduck', self.on_volume_unduck)

        except Exception:
            LOG.exception('In Mark 2 Skill')

        # Update use of wake-up beep
        self._sync_wake_beep_setting()

        self.settings.set_changed_callback(self.on_websettings_changed)

    ###################################################################
    # System events
    # TODO Check if these are needed or if the mycroft-admin-service handles
    # these
    def handle_system_reboot(self, message):
        self.speak_dialog('rebooting', wait=True)
        subprocess.call(['/usr/bin/systemctl', 'reboot'])

    def handle_system_shutdown(self, message):
        subprocess.call(['/usr/bin/systemctl', 'poweroff'])

    def handle_show_text(self, message):
        self.log.debug("Drawing text to framebuffer")
        self.showing = True
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
        self.showing = False

    ###################################################################
    # System volume

    def on_volume_set(self, message):
        """ Force vol between 0.0 and 1.0. """
        vol = message.data.get("percent", 0.5)
        vol = 0.0 if vol < 0.0 else vol
        vol = 1.0 if vol > 1.0 else vol
        self.volume = vol
        self.muted = False
        self.set_hardware_volume(vol)
        self.show_volume = True

    def on_volume_get(self, message):
        """ Handle request for current volume. """
        self.bus.emit(message.response(data={'percent': self.volume,
                                             'muted': self.muted}))
        self.show_volume = message.data.get('show', False)

    def on_volume_duck(self, message):
        """ Handle ducking event by setting the output to 0. """
        self.muted = True
        self.set_hardware_volume(0)

    def on_volume_unduck(self, message):
        """ Handle ducking event by setting the output to previous value. """
        self.muted = False
        self.set_hardware_volume(self.volume)

    def set_hardware_volume(self, pct):
        """ Set the volume on hardware (which supports levels 0-63).

            Arguments:
                pct (int): audio volume (0.0 - 1.0).
        """
        self.log.debug('Setting hardware volume to: {}'.format(pct))
        try:
            subprocess.call(['/usr/sbin/i2cset',
                             '-y',                 # force a write
                             '1',                  # i2c bus number
                             '0x4b',               # stereo amp device address
                             str(int(15 * pct) + 15)])  # volume level, 0-63
        except Exception as e:
            self.log.error('Couldn\'t set volume. ({})'.format(e))

    def get_hardware_volume(self):
        # Get the volume from hardware
        try:
            vol = subprocess.check_output(['/usr/sbin/i2cget', '-y',
                                           '1', '0x4b'])
            # Convert the returned hex value from i2cget
            i = int(vol, 16)
            i = 0 if i < 0 else i
            i = 63 if i > 63 else i
            self.volume = i / 63.0
        except subprocess.CalledProcessError as e:
            self.log.info('I2C Communication error:  {}'.format(repr(e)))
        except FileNotFoundError:
            self.log.info('i2cget couldn\'t be found')
        except Exception:
            self.log.info('UNEXPECTED VOLUME RESULT:  {}'.format(vol))

    def reset_face(self, message):
        """Triggered after skills are initialized."""
        self.loading = False
        if is_paired():
            play_wav(join(self.root_dir, 'ui', 'bootup.wav'))
        if not self.showing:
            draw_file(self.find_resource('mycroft.fb', 'ui'))

    def shutdown(self):
        # Gotta clean up manually since not using add_event()
        self.bus.remove('mycroft.skill.handler.start',
                        self.on_handler_started)
        self.bus.remove('mycroft.skill.handler.complete',
                        self.on_handler_complete)
        self.bus.remove('recognizer_loop:audio_output_start',
                        self.on_handler_audio_start)
        self.bus.remove('recognizer_loop:audio_output_end',
                        self.on_handler_audio_end)

    def handle_ap_up(self, message):
        draw_file(self.find_resource('0-wifi-connect.fb', 'ui'))

    def handle_wifi_device_connected(self, message):
        draw_file(self.find_resource('1-wifi-follow-prompt.fb', 'ui'))
        time.sleep(10)
        draw_file(self.find_resource('2-wifi-choose-network.fb', 'ui'))

    def handle_ap_success(self, message):
        draw_file(self.find_resource('3-wifi-success.fb', 'ui'))

    def handle_paired(self, message):
        self.bus.remove('enclosure.mouth.text', self.handle_show_text)
        draw_file(self.find_resource('5-pairing-success.fb', 'ui'))
        time.sleep(5)
        draw_file(self.find_resource('6-intro.fb', 'ui'))
        time.sleep(10)
        self.reset_face()

    def on_handler_audio_start(self, message):
        """Light up LED when speaking, show volume if requested"""
        if self.show_volume:
            pixel_ring.set_volume(int(self.volume * self.num_leds))
        else:
            self.speaking = True
            pixel_ring.set_color_palette(self.main_blue, self.tertiary_blue)
            pixel_ring.speak()

    def on_handler_audio_end(self, message):
        self.speaking = False
        self.showing_volume = False
        pixel_ring.off()

    def on_handler_started(self, message):
        """When a skill begins executing turn on the LED ring"""
        handler = message.data.get('handler', '')
        if self._skip_handler(handler):
            return
        pixel_ring.set_color_palette(self.main_blue, self.tertiary_green)
        pixel_ring.think()

    def on_handler_complete(self, message):
        """When a skill finishes executing turn off the LED ring"""
        handler = message.data.get('handler', '')
        if self._skip_handler(handler):
            return

        # If speaking has already begun, on_handler_audio_end will
        # turn off the LEDs
        if not self.speaking and not self.show_volume:
            pixel_ring.off()

    def _skip_handler(self, handler):
        """Ignoring handlers from this skill and from the background clock"""
        return any(skip in handler for skip in self.skip_list)


    def handle_listener_started(self, message):
        """Light up LED when listening"""
        pixel_ring.set_color_palette(self.main_blue, self.main_blue)
        pixel_ring.listen()

    def handle_listener_ended(self, message):
        pixel_ring.off()

    def handle_failed_stt(self, message):
        """ No discernable words were transcribed. Show idle screen again. """
        pass


    #####################################################################
    # Manage network connction feedback

    def handle_internet_connected(self, message):
        """ System came online later after booting. """
        if is_paired():
            self.enclosure.mouth_reset()
        else:
            # If we are not paired the pairing process will begin.
            # Cannot handle from mycroft.not.paired event because
            # we trigger first pairing with an utterance.
            draw_file(self.find_resource('3-wifi-success.fb', 'ui'))
            time.sleep(5)
            draw_file(self.find_resource('4-pairing-home.fb', 'ui'))
            self.bus.on('enclosure.mouth.text', self.handle_show_text)

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

