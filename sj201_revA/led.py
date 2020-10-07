# Copyright 2020 Mycroft AI Inc.
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

from subprocess import Popen

class Led:
    """
    Class to manipulate the LEDs on an SJ201
    Conforms to the same interface for all Mycroft
    LED devices which support at least these functions ...

      get_led_capabilities()
          returns led capability object

      set_led(led_num, rgb_tuple)
          set led to color

      get_led(led_num)
          get the current color of a led 

      set_leds(list of 12 led rgb tuples)
          set all leds from list of rgb tuples

      get_leds()
          get a list of all led rgb tuples

      fill_leds(color)
          fill all leds to the rgb tuple

    """

    def __init__(self):
        self.num_leds = 12  # sj201 has 12
        black = (0,0,0)
        self.leds = list((black,) * self.num_leds)


    def _update_leds(self):
        cmd = "sudo "
        cmd += "/opt/mycroft/skills/skill-mark-2-pi.mycroftai/sj201_revA/"
        cmd += "sj201_revA_set_leds.py "

        cmd += " ".join(
                       map(
                           str, 
                           [item for sublist in self.leds for item in sublist]
                          )
                       )

        p = Popen(cmd, shell=True)


    def get_led_capabilities(self):
        return {"num_leds":self.num_leds, "led_type":"RGB"}


    def fill_leds(self, color):
        """ set all leds to the same color """
        self.leds = list((color,) * self.num_leds)
        self._update_leds()


    def set_leds(self, input_leds):
        """ set all leds from list of tuples """
        for x in range(self.num_leds):
            self.leds[x] = input_leds[x] 
        self._update_leds()


    def get_leds(self):
        """ get a list of color rgb tuples """
        return self.leds


    def set_led(self, which, color):
        """ set a led to some color where color is an RGB tuple """
        self.leds[which % self.num_leds] = color
        self._update_leds()


    def get_led(self, which):
        """ get the color (rgb tuple) of a particular led. """
        return self.leds[which % self.num_leds]

