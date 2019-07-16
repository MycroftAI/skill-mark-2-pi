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

from mycroft.messagebus.message import Message
from mycroft.skills.core import MycroftSkill
from mycroft.util.log import LOG
from mycroft.util.parse import normalize
from mycroft import intent_file_handler


class Mark2(MycroftSkill):
    """
        The Mark2 skill handles much of the screen and audio activities
        related to Mycroft's core functionality.
    """
    def __init__(self):
        super().__init__('Mark2')

        self.settings['auto_brightness'] = False
        self.settings['use_listening_beep'] = True

        # System volume
        self.volume = 0.5
        self.muted = False
        self.get_hardware_volume()       # read from the device

    def initialize(self):
        """ Perform initalization.

            Registers messagebus handlers.
        """
        self.brightness_dict = self.translate_namedvalues('brightness.levels')


        try:
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

            self.bus.on('enclosure.mouth.reset',
                        self.on_handler_mouth_reset)
            self.bus.on('recognizer_loop:audio_output_end',
                        self.on_handler_mouth_reset)

            self.bus.on('mycroft.skills.initialized', self.reset_face)

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

    def on_volume_get(self, message):
        """ Handle request for current volume. """
        self.bus.emit(message.response(data={'percent': self.volume,
                                             'muted': self.muted}))

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
        """ Triggered after skills are initialized.

            Sets switches from resting "face" to a registered resting screen.
        """
        time.sleep(1)
        self.collect_resting_screens()

    def shutdown(self):
        # Gotta clean up manually since not using add_event()
        self.bus.remove('mycroft.skill.handler.start',
                        self.on_handler_started)
        self.bus.remove('enclosure.mouth.reset',
                        self.on_handler_mouth_reset)
        self.bus.remove('recognizer_loop:audio_output_end',
                        self.on_handler_mouth_reset)

    def on_handler_started(self, message):
        handler = message.data.get("handler", "")
        # Ignoring handlers from this skill and from the background clock
        if 'Mark2' in handler:
            return
        if 'TimeSkill.update_display' in handler:
            return

    def on_handler_mouth_reset(self, message):
        """ Restore viseme to a smile. """
        pass

    def on_handler_complete(self, message):
        """ When a skill finishes executing clear the showing page state. """
        handler = message.data.get('handler', '')
        # Ignoring handlers from this skill and from the background clock
        # TODO: implement Something here

    def handle_listener_started(self, message):
        """ Shows listener page after wakeword is triggered.

            Starts countdown to show the idle page.
        """
        # TODO: implement Something here
        pass

    def handle_listener_ended(self, message):
        """ When listening has ended show the thinking animation. """
        pass

    def handle_failed_stt(self, message):
        """ No discernable words were transcribed. Show idle screen again. """
        pass

    #####################################################################
    # Manage network connction feedback

    def handle_internet_connected(self, message):
        """ System came online later after booting. """
        self.enclosure.mouth_reset()

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
