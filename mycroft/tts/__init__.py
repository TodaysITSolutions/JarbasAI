# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.
import hashlib
import random
from Queue import Queue, Empty
from threading import Thread
from time import time, sleep

import os
import os.path
from abc import ABCMeta, abstractmethod
from os.path import dirname, exists, isdir

import mycroft.util
from mycroft.client.enclosure.api import EnclosureAPI
from mycroft.configuration import ConfigurationManager
from mycroft.messagebus.message import Message
from mycroft.util import play_wav, play_mp3, check_for_signal, create_signal
from mycroft.util.log import LOG
import re

__author__ = 'jdorleans'


class PlaybackThread(Thread):
    """
        Thread class for playing back tts audio and sending
        visime data to enclosure.
    """

    def __init__(self, queue):
        super(PlaybackThread, self).__init__()
        self.queue = queue
        self._terminated = False
        self._processing_queue = False
        self._clear_visimes = False

    def init(self, tts):
        self.tts = tts

    def clear_queue(self):
        """
            Remove all pending playbacks.
        """
        while not self.queue.empty():
            self.queue.get()
        try:
            self.p.terminate()
        except:
            pass

    def run(self):
        """
            Thread main loop. get audio and visime data from queue
            and play.
        """
        while not self._terminated:
            try:
                snd_type, data, visimes = self.queue.get(timeout=2)
                self.blink(0.5)
                if not self._processing_queue:
                    self._processing_queue = True
                    self.tts.begin_audio()

                if snd_type == 'wav':
                    self.p = play_wav(data)
                elif snd_type == 'mp3':
                    self.p = play_mp3(data)

                if visimes:
                    if self.show_visimes(visimes):
                        self.clear_queue()
                else:
                    self.p.communicate()
                self.p.wait()

                if self.queue.empty():
                    self.tts.end_audio()
                    self._processing_queue = False
                self.blink(0.2)
            except Empty:
                pass
            except Exception, e:
                LOG.exception(e)
                if self._processing_queue:
                    self.tts.end_audio()
                    self._processing_queue = False

    def show_visimes(self, pairs):
        """
            Send visime data to enclosure

            Args:
                pairs(list): Visime and timing pair

            Returns:
                True if button has been pressed.
        """
        start = time()
        for code, duration in pairs:
            if self._clear_visimes:
                self._clear_visimes = False
                return True
            if self.enclosure:
                self.enclosure.mouth_viseme(code)
            delta = time() - start
            if delta < duration:
                sleep(duration - delta)
        return False

    def clear_visimes(self):
        self._clear_visimes = True

    def blink(self, rate=1.0):
        """ Blink mycroft's eyes """
        if self.enclosure and random.random() < rate:
            self.enclosure.eyes_blink("b")

    def stop(self):
        """ Stop thread """
        self._terminated = True
        self.clear_queue()


class TTS(object):
    """
    TTS abstract class to be implemented by all TTS engines.

    It aggregates the minimum required parameters and exposes
    ``execute(sentence)`` and ``validate_ssml(sentence)`` functions.
    """
    __metaclass__ = ABCMeta

    def __init__(self, lang, config, validator):
        super(TTS, self).__init__()
        self.lang = lang or 'en-us'
        self.config = config
        self.voice = config.get("voice")
        self.filename = '/tmp/tts.wav'
        self.validator = validator
        self.enclosure = None
        random.seed()
        self.queue = Queue()
        self.playback = PlaybackThread(self.queue)
        self.playback.start()
        self.clear_cache()
        self.ssml_support = self.config.get("ssml", False)
        default_tags = ["speak", "lang", "p", "phoneme", "prosody", "break",
                        "sub"]
        # check for engine overrided default supported tags
        self.supported_tags = self.config.get("supported_tags", default_tags)
        # extra engine specific tags
        self.extra_tags = self.config.get("extra_tags", [])

    def begin_audio(self):
        """Helper function for child classes to call in execute()"""
        self.ws.emit(Message("recognizer_loop:audio_output_start"))
        create_signal("isSpeaking")

    def end_audio(self):
        """Helper function for child classes to call in execute()"""
        self.ws.emit(Message("recognizer_loop:audio_output_end"))

        # This check will clear the "signal"
        check_for_signal("isSpeaking")

    def init(self, ws):
        self.ws = ws
        self.playback.init(self)
        self.enclosure = EnclosureAPI(self.ws)
        self.playback.enclosure = self.enclosure

    def get_tts(self, sentence, wav_file):
        """
            Abstract method that a tts implementation needs to implement.
            Should get data from tts.

            Args:
                sentence(str): Sentence to synthesize
                wav_file(str): output file

            Returns: (wav_file, phoneme) tuple
        """
        pass

    def validate_ssml(self, utterance):
        """
            Check if engine supports ssml, if not remove all tags
            Remove unsupported / invalid tags

            Args:
                sentence(str): Sentence to validate

            Returns: validated_sentence (str)
        """
        # if ssml is not supported by TTS engine remove all tags
        if not self.ssml_support:
            return re.sub('<[^>]*>', '', utterance)

        supported_tags = self.supported_tags + self.extra_tags

        # find ssml tags in string
        tags = re.findall('<[^>]*>', utterance)

        for tag in tags:
            flag = False  # not supported
            for supported in supported_tags:
                if supported in tag:
                    flag = True  # supported
            if not flag:
                # remove unsupported tag
                utterance = utterance.replace(tag, "")

        # return text with supported ssml tags only
        return utterance.replace("  ", " ")

    def validate_and_execute(self, sentence):
        """
            validate ssml, execute text to speech

            Args:
                sentence(str): Sentence to execute
        """
        sentence = self.validate_ssml(sentence)
        self.execute(sentence)

    def execute(self, sentence):
        """
            Convert sentence to speech.

            The method caches results if possible using the hash of the
            sentence.

            Args:
                sentence:   Sentence to be spoken
        """
        key = str(hashlib.md5(sentence.encode('utf-8', 'ignore')).hexdigest())
        wav_file = os.path.join(mycroft.util.get_cache_directory("tts"),
                                key + '.' + self.type)

        if os.path.exists(wav_file):
            LOG.debug("TTS cache hit")
            phonemes = self.load_phonemes(key)
        else:
            wav_file, phonemes = self.get_tts(sentence, wav_file)
            if phonemes:
                self.save_phonemes(key, phonemes)

        self.queue.put((self.type, wav_file, self.visime(phonemes)))

    def visime(self, phonemes):
        """
            Create visimes from phonemes. Needs to be implemented for all
            tts backend

            Args:
                phonemes(str): String with phoneme data
        """
        return None

    def clear_cache(self):
        """ Remove all cached files. """
        if not os.path.exists(mycroft.util.get_cache_directory('tts')):
            return
        for f in os.listdir(mycroft.util.get_cache_directory("tts")):
            file_path = os.path.join(mycroft.util.get_cache_directory("tts"),
                                     f)
            if os.path.isfile(file_path):
                os.unlink(file_path)

    def save_phonemes(self, key, phonemes):
        """
            Cache phonemes

            Args:
                key:        Hash key for the sentence
                phonemes:   phoneme string to save
        """
        # Clean out the cache as needed
        cache_dir = mycroft.util.get_cache_directory("tts")
        mycroft.util.curate_cache(cache_dir)

        pho_file = os.path.join(cache_dir, key + ".pho")
        try:
            with open(pho_file, "w") as cachefile:
                cachefile.write(phonemes)
        except:
            LOG.debug("Failed to write .PHO to cache")
            pass

    def load_phonemes(self, key):
        """
            Load phonemes from cache file.

            Args:
                Key:    Key identifying phoneme cache
        """
        pho_file = os.path.join(mycroft.util.get_cache_directory("tts"),
                                key + ".pho")
        if os.path.exists(pho_file):
            try:
                with open(pho_file, "r") as cachefile:
                    phonemes = cachefile.read().strip()
                return phonemes
            except:
                LOG.debug("Failed to read .PHO from cache")
        return None

    def __del__(self):
        self.playback.stop()
        self.playback.join()


class TTSValidator(object):
    """
    TTS Validator abstract class to be implemented by all TTS engines.

    It exposes and implements ``validate(tts)`` function as a template to
    validate the TTS engines.
    """
    __metaclass__ = ABCMeta

    def __init__(self, tts):
        self.tts = tts

    def validate(self):
        self.validate_instance()
        self.validate_filename()
        self.validate_lang()
        self.validate_connection()

    def validate_instance(self):
        clazz = self.get_tts_class()
        if not isinstance(self.tts, clazz):
            raise AttributeError('tts must be instance of ' + clazz.__name__)

    def validate_filename(self):
        filename = self.tts.filename
        if not (filename and filename.endswith('.wav')):
            raise AttributeError('file: %s must be in .wav format!' % filename)

        dir_path = dirname(filename)
        if not (exists(dir_path) and isdir(dir_path)):
            raise AttributeError('filename: %s is not valid!' % filename)

    @abstractmethod
    def validate_lang(self):
        pass

    @abstractmethod
    def validate_connection(self):
        pass

    @abstractmethod
    def get_tts_class(self):
        pass


class TTSFactory(object):
    from mycroft.tts.espeak_tts import ESpeak
    from mycroft.tts.fa_tts import FATTS
    from mycroft.tts.google_tts import GoogleTTS
    from mycroft.tts.mary_tts import MaryTTS
    from mycroft.tts.mimic_tts import Mimic
    from mycroft.tts.spdsay_tts import SpdSay
    from mycroft.tts.morse_code_tts import MorseCode
    from mycroft.tts.beepspeak_tts import BeepSpeak
    from mycroft.tts.pico_tts import Pico
    from mycroft.tts.deep_throat_tts import DeepThroat
    from mycroft.tts.tacotron_tts import Tacotron
    from mycroft.tts.polly_tts import Polly
    from mycroft.tts.bing_tts import BingTTS
    from mycroft.tts.ibm_tts import WatsonTTS
    #from mycroft.tts.pymimic_tts import Pymimic

    CLASSES = {
        "mimic": Mimic,
        "google": GoogleTTS,
        "marytts": MaryTTS,
        "fatts": FATTS,
        "espeak": ESpeak,
        "spdsay": SpdSay,
        #"pymimic": Pymimic,
        "morse": MorseCode,
        "beep_speak": BeepSpeak,
        "pico": Pico,
        "deep_throat": DeepThroat,
        "tacotron": Tacotron,
        "polly": Polly,
        "bing": BingTTS,
        "ibm": WatsonTTS
    }

    @staticmethod
    def create():
        """
        Factory method to create a TTS engine based on configuration.

        The configuration file ``mycroft.conf`` contains a ``tts`` section with
        the name of a TTS module to be read by this method.

        "tts": {
            "module": <engine_name>
        }
        """
        config = ConfigurationManager.get()
        lang = config.get("lang", "en-us")
        tts_module = config.get('tts', {}).get('module', 'mimic')
        tts_config = config.get('tts', {}).get(tts_module, {})
        tts_lang = tts_config.get('lang', lang)
        clazz = TTSFactory.CLASSES.get(tts_module)
        tts = clazz(tts_lang, tts_config)
        tts.validator.validate()
        return tts
