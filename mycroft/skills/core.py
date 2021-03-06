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
import abc
import imp
import time
import sys

import operator
import os.path
import re
import time
from os.path import join, dirname, splitext, isdir

from os.path import join, abspath, dirname, splitext, isdir, \
    basename, exists
from os import listdir
from functools import wraps

from adapt.intent import Intent, IntentBuilder

from mycroft.client.enclosure.api import EnclosureAPI
from mycroft.configuration import ConfigurationManager
from mycroft.dialog import DialogLoader
from mycroft.filesystem import FileSystemAccess
from mycroft.messagebus.message import Message
from mycroft.util.log import getLogger
from mycroft.skills.settings import SkillSettings
from mycroft import MYCROFT_ROOT_PATH

from inspect import getargspec

__author__ = 'seanfitz'

skills_config = ConfigurationManager.instance().get("skills")
config_dir = skills_config.get("directory", "default")
if config_dir == "default":
    SKILLS_DIR = join(MYCROFT_ROOT_PATH, "jarbas_skills")
else:
    SKILLS_DIR = config_dir

MainModule = '__init__'

logger = getLogger(__name__)


def load_vocab_from_file(path, vocab_type, emitter):
    """
        Load mycroft vocabulary from file. and send it on the message bus for
        the intent handler.

        Args:
            path:       path to vocabulary file (*.voc)
            vocab_type: keyword name
            emitter:    emitter to access the message bus
    """
    if path.endswith('.voc'):
        with open(path, 'r') as voc_file:
            for line in voc_file.readlines():
                parts = line.strip().split("|")
                entity = parts[0]

                emitter.emit(Message("register_vocab", {
                    'start': entity, 'end': vocab_type
                }))
                for alias in parts[1:]:
                    emitter.emit(Message("register_vocab", {
                        'start': alias, 'end': vocab_type, 'alias_of': entity
                    }))


def load_regex_from_file(path, emitter):
    """
        Load regex from file and send it on the message bus for
        the intent handler.

        Args:
            path:       path to vocabulary file (*.voc)
            emitter:    emitter to access the message bus
    """
    if path.endswith('.rx'):
        with open(path, 'r') as reg_file:
            for line in reg_file.readlines():
                re.compile(line.strip())
                emitter.emit(
                    Message("register_vocab", {'regex': line.strip()}))


def load_vocabulary(basedir, emitter):
    for vocab_type in listdir(basedir):
        if vocab_type.endswith(".voc"):
            load_vocab_from_file(
                join(basedir, vocab_type), splitext(vocab_type)[0], emitter)


def load_regex(basedir, emitter):
    for regex_type in listdir(basedir):
        if regex_type.endswith(".rx"):
            load_regex_from_file(
                join(basedir, regex_type), emitter)


def open_intent_envelope(message):
    """ Convert dictionary received over messagebus to Intent. """
    intent_dict = message.data
    return Intent(intent_dict.get('name'),
                  intent_dict.get('requires'),
                  intent_dict.get('at_least_one'),
                  intent_dict.get('optional'))


def load_skill(skill_descriptor, emitter, skill_id, BLACKLISTED_SKILLS=None):
    """
        load skill from skill descriptor.

        Args:
            skill_descriptor: descriptor of skill to load
            emitter:          messagebus emitter
            skill_id:         id number for skill
    """
    BLACKLISTED_SKILLS = BLACKLISTED_SKILLS or []
    try:
        logger.info("ATTEMPTING TO LOAD SKILL: " + skill_descriptor["name"] +
                    " with ID " + str(skill_id))
        if skill_descriptor['name'] in BLACKLISTED_SKILLS:
            logger.info("SKILL IS BLACKLISTED " + skill_descriptor["name"])
            return None
        skill_module = imp.load_module(
            skill_descriptor["name"] + MainModule, *skill_descriptor["info"])
        if (hasattr(skill_module, 'create_skill') and
                callable(skill_module.create_skill)):
            # v2 skills framework
            skill = skill_module.create_skill()
            if not skill.is_current_language_supported():
                logger.info("SKILL DOES NOT SUPPORT CURRENT LANGUAGE")
                return None
            skill.bind(emitter)
            skill.skill_id = skill_id
            skill.load_data_files(dirname(skill_descriptor['info'][1]))
            # Set up intent handlers
            skill.initialize()
            logger.info(
                "Loaded " + skill_descriptor["name"] + " with ID " + str(
                    skill_id))
            skill._register_decorated()
            return skill
        else:
            logger.warn(
                "Module %s does not appear to be skill" % (
                    skill_descriptor["name"]))
    except:
        logger.error(
            "Failed to load skill: " + skill_descriptor["name"],
            exc_info=True)
    return None


def get_skills(skills_folder):
    logger.info("LOADING SKILLS FROM " + skills_folder)
    skills = []
    possible_skills = os.listdir(skills_folder)
    for i in possible_skills:
        location = join(skills_folder, i)
        if (isdir(location) and
                not MainModule + ".py" in os.listdir(location)):
            for j in os.listdir(location):
                name = join(location, j)
                if (not isdir(name) or
                        not MainModule + ".py" in os.listdir(name)):
                    continue
                skills.append(create_skill_descriptor(name))
        if (not isdir(location) or
                not MainModule + ".py" in os.listdir(location)):
            continue

        skills.append(create_skill_descriptor(location))
    skills = sorted(skills, key=lambda p: p.get('name'))
    return skills


def create_skill_descriptor(skill_folder):
    info = imp.find_module(MainModule, [skill_folder])
    return {"name": basename(skill_folder), "info": info}


def get_handler_name(handler):
    """
        Return name (including class if available) of handler
        function.

        Args:
            handler (function): Function to be named

        Returns: handler name as string
    """
    name = ''
    if '__self__' in dir(handler) and \
                    'name' in dir(handler.__self__):
        name += handler.__self__.name + '.'
    name += handler.__name__
    return name


# Lists used when adding skill handlers using decorators
_intent_list = []
_intent_file_list = []


def intent_handler(intent_parser):
    """ Decorator for adding a method as an intent handler. """

    def real_decorator(func):
        @wraps(func)
        def handler_method(*args, **kwargs):
            return func(*args, **kwargs)

        _intent_list.append((intent_parser, func))
        return handler_method

    return real_decorator


def intent_file_handler(intent_file):
    """ Decorator for adding a method as an intent file handler. """

    def real_decorator(func):
        @wraps(func)
        def handler_method(*args, **kwargs):
            return func(*args, **kwargs)

        _intent_file_list.append((intent_file, func))
        return handler_method

    return real_decorator


class MycroftSkill(object):
    """
    Abstract base class which provides common behaviour and parameters to all
    Skills implementation.
    """

    def __init__(self, name=None, emitter=None):
        self.name = name or self.__class__.__name__
        # Get directory of skill
        self._dir = dirname(abspath(sys.modules[self.__module__].__file__))

        self.bind(emitter)
        self.config_core = ConfigurationManager.get()
        self.APIS = self.config_core.get("APIS", {})
        self.config = self.config_core.get(self.name, {})
        self.dialog_renderer = None
        self.vocab_dir = None
        self.file_system = FileSystemAccess(join('skills', self.name))
        self.registered_intents = []
        self.log = getLogger(self.name)
        self.reload_skill = True
        self.external_reload = True
        self.external_shutdown = True
        self.events = []
        self.skill_id = 0
        self.message_context = {}
        self.message_context = self.get_message_context()

    def is_current_language_supported(self):
        # if skill does not use vocab/regex it supports all languages
        # if vocab/regex folder exists we can assume it supports language
        if exists(join(self._dir, 'vocab', self.lang)) or \
                exists(join(self._dir, 'regex', self.lang)) or \
                (not exists(join(self._dir, 'vocab')) and not
                exists(join(self._dir, 'regex'))):
            return True
        return False

    @property
    def location(self):
        """ Get the JSON data struction holding location information. """
        # TODO: Allow Enclosure to override this for devices that
        # contain a GPS.
        return self.config_core.get('location')

    @property
    def location_pretty(self):
        """ Get a more 'human' version of the location as a string. """
        loc = self.location
        if type(loc) is dict and loc["city"]:
            return loc["city"]["name"]
        return None

    @property
    def location_timezone(self):
        """ Get the timezone code, such as 'America/Los_Angeles' """
        loc = self.location
        if type(loc) is dict and loc["timezone"]:
            return loc["timezone"]["code"]
        return None

    @property
    def lang(self):
        return self.config_core.get('lang')

    @property
    def settings(self):
        """ Load settings if not already loaded. """
        try:
            return self._settings
        except:
            try:
                self._settings = SkillSettings(self._dir)
            except:
                self._settings = SkillSettings(dirname(__file__))
            return self._settings

    def bind(self, emitter):
        """ Register emitter with skill. """
        if emitter:
            self.emitter = emitter
            self.enclosure = EnclosureAPI(emitter, self.name)
            self.__register_stop()
            self.emitter.on('enable_intent', self.handle_enable_intent)
            self.emitter.on('disable_intent', self.handle_disable_intent)

    def __register_stop(self):
        self.stop_time = time.time()
        self.stop_threshold = self.config_core.get("skills").get(
            'stop_threshold')
        self.add_event('mycroft.stop', self.__handle_stop, False)

    def detach(self):
        for (name, intent) in self.registered_intents:
            name = str(self.skill_id) + ':' + name
            self.emitter.emit(Message("detach_intent", {"intent_name": name}))

    def request_reload(self):
        self.emitter.emit(
            Message("reload_skill_request", {"skill_id": self.skill_id}))

    def request_shutdown(self):
        self.emitter.emit(
            Message("shutdown_skill_request", {"skill_id": self.skill_id}))

    def initialize(self):
        """
        Initialization function to be implemented by all Skills.

        Usually used to create intents rules and register them.
        """
        logger.debug("No initialize function implemented")

    def converse(self, utterances, lang="en-us"):
        """
            Handle conversation. This method can be used to override the normal
            intent handler after the skill has been invoked once.

            To enable this override thise converse method and return True to
            indicate that the utterance has been handled.

            Args:
                utterances: The utterances from the user
                lang:       language the utterance is in

            Returns:    True if an utterance was handled, otherwise False
        """
        return False

    def make_active(self):
        """
            Bump skill to active_skill list in intent_service
            this enables converse method to be called even without skill being
            used in last 5 minutes
        """
        self.emitter.emit(Message('active_skill_request',
                                  {"skill_id": self.skill_id}))

    def _register_decorated(self):
        """
        Register all intent handlers that has been decorated with an intent.
        """
        global _intent_list, _intent_file_list
        for intent_parser, handler in _intent_list:
            self.register_intent(intent_parser, handler, need_self=True)
        for intent_file, handler in _intent_file_list:
            self.register_intent_file(intent_file, handler, need_self=True)
        _intent_list = []
        _intent_file_list = []

    def add_event(self, name, handler, need_self=False):
        """
                  Create event handler for executing intent

                  Args:
                      name:       IntentParser name
                      handler:    method to call
                      need_self:     optional parameter, when called from a decorated
                                     intent handler the function will need the self
                                     variable passed as well.
              """

        def wrapper(message):
            try:
                # Indicate that the skill handler is starting
                name = get_handler_name(handler)
                self.emitter.emit(Message("mycroft.skill.handler.start",
                                          data={'handler': name,
                                                "intent": message.type,
                                                "data": message.data,
                                                "context": message.context}))
                if need_self:
                    # When registring from decorator self is required
                    if len(getargspec(handler).args) == 2:
                        handler(self, message)
                    elif len(getargspec(handler).args) == 1:
                        handler(self)
                    else:
                        raise TypeError
                else:
                    if len(getargspec(handler).args) == 2:
                        handler(message)
                    elif len(getargspec(handler).args) == 1:
                        handler()
                    else:
                        raise TypeError
                self.settings.store()  # Store settings if they've changed
            except Exception as e:
                # TODO: Localize
                self.speak(
                    "An error occurred while processing a request in " +
                    self.name)
                logger.error(
                    "An error occurred while processing a request in " +
                    self.name, exc_info=True)
                # indicate completion with exception
                self.emitter.emit(Message('mycroft.skill.handler.complete',
                                          data={'handler': name,
                                                'exception': e.message,
                                                "intent": message.type,
                                                "data": message.data,
                                                "context": message.context}))
            # Indicate that the skill handler has completed
            self.emitter.emit(Message('mycroft.skill.handler.complete',
                                      data={'handler': name,
                                            "intent": message.type,
                                            "data": message.data,
                                            "context": message.context}))

        if handler:
            self.emitter.on(name, self.handle_update_message_context)
            self.emitter.on(name, wrapper)
            self.events.append((name, wrapper))

    def register_intent(self, intent_parser, handler, need_self=False):
        """
                    Register an Intent with the intent service.

                    Args:
                        intent_parser: Intent or IntentBuilder object to parse
                                       utterance for the handler.
                        handler:       function to register with intent
                        need_self:     optional parameter, when called from a decorated
                                       intent handler the function will need the self
                                       variable passed as well.
                """
        if type(intent_parser) == IntentBuilder:
            intent_parser = intent_parser.build()
        elif type(intent_parser) != Intent:
            raise ValueError('intent_parser is not an Intent')

        name = intent_parser.name
        intent_parser.name = str(self.skill_id) + ':' + intent_parser.name
        self.emitter.emit(Message("register_intent", intent_parser.__dict__))
        self.registered_intents.append((name, intent_parser))
        self.add_event(intent_parser.name, handler)

    def register_intent_file(self, intent_file, handler, need_self=False):
        """
                  Register an Intent file with the intent service.

                  Args:
                      intent_file: name of file that contains example queries
                                   that should activate the intent
                      handler:     function to register with intent
                      need_self:   use for decorator. See register_intent
              """

        intent_name = str(self.skill_id) + ':' + intent_file
        self.emitter.emit(Message("padatious:register_intent", {
            "file_name": join(self.vocab_dir, intent_file),
            "intent_name": intent_name
        }))
        self.add_event(intent_name, handler)

    def handle_update_message_context(self, message):
        self.message_context = self.get_message_context(message.context)

    def disable_intent(self, intent_name):
        """Disable a registered intent"""
        for (name, intent) in self.registered_intents:
            if name == intent_name:
                logger.debug('Disabling intent ' + intent_name)
                name = str(self.skill_id) + ':' + intent_name
                self.emitter.emit(
                    Message("detach_intent", {"intent_name": name}))
                return

    def enable_intent(self, intent_name):
        """Reenable a registered intent"""
        for (name, intent) in self.registered_intents:
            if name == intent_name:
                self.registered_intents.remove((name, intent))
                intent.name = name
                self.register_intent(intent, None)
                logger.info("Enabling Intent " + intent_name)
                return

    def handle_enable_intent(self, message):
        intent_name = message.data["intent_name"]
        self.enable_intent(intent_name)

    def handle_disable_intent(self, message):
        intent_name = message.data["intent_name"]
        self.disable_intent(intent_name)

    def set_context(self, context, word=''):
        """
            Add context to intent service

            Args:
                context:    Keyword
                word:       word connected to keyword
        """
        if not isinstance(context, basestring):
            raise ValueError('context should be a string')
        if not isinstance(word, basestring):
            raise ValueError('word should be a string')
        self.emitter.emit(Message('add_context', {'context': context, 'word':
            word}))

    def remove_context(self, context):
        """
            remove_context removes a keyword from from the context manager.
        """
        if not isinstance(context, basestring):
            raise ValueError('context should be a string')
        self.emitter.emit(Message('remove_context', {'context': context}))

    def register_vocabulary(self, entity, entity_type):
        """ Register a word to an keyword

            Args:
                entity:         word to register
                entity_type:    Intent handler entity to tie the word to
        """
        self.emitter.emit(Message('register_vocab', {
            'start': entity, 'end': entity_type
        }))

    def register_regex(self, regex_str):
        re.compile(regex_str)  # validate regex
        self.emitter.emit(Message('register_vocab', {'regex': regex_str}))

    def get_message_context(self, message_context=None):
        if message_context is None:
            message_context = {
                "destinatary": self.message_context.get("destinatary", "all"),
                "mute": False, "more_speech": False,
                "target": self.message_context.get("target", "all")}
        else:
            if "destinatary" not in message_context.keys():
                message_context["destinatary"] = self.message_context.get(
                    "destinatary", "all")
            if "target" not in message_context.keys():
                message_context["target"] = self.message_context.get("target",
                                                                     "all")
            if "mute" not in message_context.keys():
                message_context["mute"] = self.message_context.get("mute",
                                                                   False)
            if "more_speech" not in message_context.keys():
                message_context["more_speech"] = self.message_context.get(
                    "more_speech", False)
        message_context["source"] = self.name
        return message_context

    def check_for_ssml(self, text):
        """ checks if current TTS engine supports SSML , if it doesn't
        removes all SSML tags, if it does removes unsupported SSML tags,
        returns processed text """

        module = self.config_core.get("tts", {}).get("module")
        config = self.config_core.get("tts", {}).get(module, {})
        ssml_support = config.get("ssml", False)

        # if ssml is not supported by TTS engine remove all tags
        if not ssml_support:
            return re.sub('<[^>]*>', '', text)

        # default ssml tags all engines should support
        default_tags = ["speak", "lang", "p", "phoneme", "prosody", "s",
                        "say-as", "sub", "w"]
        all_tags = self.config_core.get("ssml_tags", default_tags)
        # check for engine overrided default supported tags
        supported_tags = config.get("supported_tags", all_tags)
        # engine supported tags
        extra_tags = config.get("extra_tags", ["drc", "whispered"])
        supported_tags = supported_tags + extra_tags

        # find tags in string
        tags = re.findall('<[^>]*>', text)

        for tag in tags:
            flag = False  # not supported
            for supported in supported_tags:
                if supported in tag:
                    flag = True  # supported
            if not flag:
                # remove unsupported tag
                text = text.replace(tag, "")

        # return text with supported ssml tags only
        return text

    def speak(self, utterance, expect_response=False, metadata=None,
              message_context=None):
        """
                    Speak a sentence.

                    Args:
                        utterance:          sentence mycroft should speak
                        expect_response:    set to True if Mycroft should expect a
                                            response from the user and start listening
                                            for response.
                """
        if message_context is None:
            # use current context
            message_context = self.message_context
        if metadata is None:
            metadata = {}
        # registers the skill as being active
        self.enclosure.register(self.name)
        # check utterance for ssml
        utterance = self.check_for_ssml(utterance)
        data = {'utterance': utterance,
                'expect_response': expect_response,
                "metadata": metadata}
        self.emitter.emit(
            Message("speak", data, self.get_message_context(message_context)))
        self.set_context('Last_Speech', utterance)
        for field in metadata:
            self.set_context(field, str(metadata[field]))

    def speak_dialog(self, key, data=None, expect_response=False,
                     metadata=None, message_context=None):
        """
                   Speak sentance based of dialog file.

                   Args
                       key: dialog file key (filname without extension)
                       data: information to populate sentence with
                       expect_response:    set to True if Mycroft should expect a
                                           response from the user and start listening
                                           for response.
               """
        if data is None:
            data = {}
        self.speak(self.dialog_renderer.render(key, data),
                   expect_response=expect_response, metadata=metadata,
                   message_context=message_context)

    def init_dialog(self, root_directory):
        dialog_dir = join(root_directory, 'dialog', self.lang)
        if os.path.exists(dialog_dir):
            self.dialog_renderer = DialogLoader().load(dialog_dir)
        else:
            logger.debug(
                'No dialog loaded, ' + dialog_dir + ' does not exist')

    def load_data_files(self, root_directory):
        self.init_dialog(root_directory)
        self.load_vocab_files(join(root_directory, 'vocab', self.lang))
        regex_path = join(root_directory, 'regex', self.lang)
        if exists(regex_path):
            self.load_regex_files(regex_path)

    def load_vocab_files(self, vocab_dir):
        self.vocab_dir = vocab_dir
        if exists(vocab_dir):
            load_vocabulary(vocab_dir, self.emitter)
        else:
            logger.debug('No vocab loaded, ' + vocab_dir + ' does not exist')

    def load_regex_files(self, regex_dir):
        load_regex(regex_dir, self.emitter)

    def __handle_stop(self, event):
        """
            Handler for the "mycroft.stop" signal. Runs the user defined
            `stop()` method.
        """
        self.stop_time = time.time()
        try:
            self.stop()
        except:
            logger.error("Failed to stop skill: {}".format(self.name),
                         exc_info=True)

    @abc.abstractmethod
    def stop(self):
        pass

    def config_update(self, config=None, save=False, isSystem=False):
        if config is None:
            config = {}
        if save:
            ConfigurationManager.save(config, isSystem)
        self.emitter.emit(
            Message("configuration.patch", {"config": config}))

    def is_stop(self):
        passed_time = time.time() - self.stop_time
        return passed_time < self.stop_threshold

    def shutdown(self):
        """
        This method is intended to be called during the skill
        process termination. The skill implementation must
        shutdown all processes and operations in execution.
        """
        # Store settings
        self.settings.store()

        # removing events
        for e, f in self.events:
            self.emitter.remove(e, f)
        self.events = None  # Remove reference to wrappers

        self.emitter.emit(
            Message("detach_skill", {"skill_id": str(self.skill_id) + ":"}))
        try:
            self.stop()
        except:
            logger.error("Failed to stop skill: {}".format(self.name),
                         exc_info=True)

    def _schedule_event(self, handler, when, data=None, name=None,
                        repeat=None):
        """
            Underlying method for schedle_event and schedule_repeating_event.
            Takes scheduling information and sends it of on the message bus.
        """
        data = data or {}
        if not name:
            name = self.name + handler.__name__
        name = str(self.skill_id) + ':' + name
        self.add_event(name, handler, False)
        event_data = {}
        event_data['time'] = time.mktime(when.timetuple())
        event_data['event'] = name
        event_data['repeat'] = repeat
        event_data['data'] = data
        self.emitter.emit(Message('mycroft.scheduler.schedule_event',
                                  data=event_data))

    def schedule_event(self, handler, when, data=None, name=None):
        """
            Schedule a single event.

            Args:
                handler:               method to be called
                when (datetime):       when the handler should be called
                data (dict, optional): data to send when the handler is called
                name (str, optional):  friendly name parameter
        """
        data = data or {}
        self._schedule_event(handler, when, data, name)

    def schedule_repeating_event(self, handler, when, frequency,
                                 data=None, name=None):
        """
            Schedule a repeating event.

            Args:
                handler:                method to be called
                when (datetime):        time for calling the handler
                frequency (float/int):  time in seconds between calls
                data (dict, optional):  data to send along to the handler
                name (str, optional):   friendly name parameter
        """
        data = data or {}
        self._schedule_event(handler, when, data, name, frequency)

    def update_event(self, name, data=None):
        """
            Change data of event.

            Args:
                name (str):   Name of event
        """
        data = data or {}
        data = {
            'event': name,
            'data': data
        }
        self.emitter.emit(Message('mycroft.schedule.update_event',
                                  data=data))

    def cancel_event(self, name):
        """
            Cancel a pending event. The event will no longer be scheduled
            to be executed

            Args:
                name (str):   Name of event
        """
        data = {'event': name}
        self.emitter.emit(Message('mycroft.scheduler.remove_event',
                                  data=data))


class FallbackSkill(MycroftSkill):
    """
        FallbackSkill is used to declare a fallback to be called when
        no skill is matching an intent. The fallbackSkill implements a
        number of fallback handlers to be called in an order determined
        by their priority.
    """
    fallback_handlers = {}
    folders = {}
    override = skills_config.get("fallback_override", False)
    order = skills_config.get("fallback_priority", [])

    def __init__(self, name=None, emitter=None):
        MycroftSkill.__init__(self, name, emitter)

        #  list of fallback handlers registered by this instance
        self.instance_fallback_handlers = []

    @classmethod
    def make_intent_failure_handler(cls, ws):
        """Goes through all fallback handlers until one returns True"""

        def ordered_handler(message):
            logger.info("Overriding fallback order")
            logger.info("Fallback order " + str(cls.order))
            missing_folders = cls.folders.keys()
            logger.info("Fallbacks " + str(missing_folders))

            # try fallbacks in ordered list
            for folder in cls.order:
                for f in cls.folders.keys():
                    logger.info(folder + " " + f)
                    if folder == f:
                        if f in missing_folders:
                            missing_folders.remove(f)
                        logger.info("Trying ordered fallback: " + folder)
                        handler = cls.folders[f]
                        try:
                            handler.__self__.handle_update_message_context(
                                message)
                            if handler(message):
                                return True
                        except Exception as e:
                            logger.info(
                                'Exception in fallback: ' +
                                handler.__self__.name + " " +
                                str(e))

            # try fallbacks missing from ordered list
            logger.info("Missing fallbacks " + str(missing_folders))
            for folder in missing_folders:
                logger.info("fallback not in ordered list, trying it now: " +
                            folder)
                handler = cls.folders[folder]
                try:
                    handler.__self__.handle_update_message_context(
                        message)
                    if handler(message):
                        return True
                except Exception as e:
                    logger.info('Exception in fallback: ' +
                                handler.__self__.name + " " +
                                str(e))
            return False

        def priority_handler(message):
            # try fallbacks by priority
            for _, handler in sorted(cls.fallback_handlers.items(),
                                     key=operator.itemgetter(0)):
                try:
                    handler.__self__.handle_update_message_context(
                        message)
                    if handler(message):
                        return True
                except Exception as e:
                    logger.info('Exception in fallback: ' +
                                handler.__self__.name + " " +
                                str(e))
            return False

        def handler(message):
            if cls.override:
                success = ordered_handler(message)
            else:
                success = priority_handler(message)
            if not success:
                ws.emit(Message('complete_intent_failure'))
                logger.warn('No fallback could handle intent.')

        return handler

    @classmethod
    def _register_fallback(cls, handler, priority, skill_folder=None):
        """
        Register a function to be called as a general info fallback
        Fallback should receive message and return
        a boolean (True if succeeded or False if failed)

        Lower priority gets run first
        0 for high priority 100 for low priority
        """
        while priority in cls.fallback_handlers:
            priority += 1

        cls.fallback_handlers[priority] = handler

        # folder name
        if skill_folder:
            skill_folder = skill_folder.split("/")[-1]
            cls.folders[skill_folder] = handler
        else:
            logger.warning("skill folder error registering fallback")

    def register_fallback(self, handler, priority):
        """
            register a fallback with the list of fallback handlers
            and with the list of handlers registered by this instance
        """
        self.instance_fallback_handlers.append(handler)
        # folder path
        try:
            skill_folder = self._dir
        except:
            skill_folder = dirname(__file__)  # skill
        self._register_fallback(handler, priority, skill_folder)

    @classmethod
    def remove_fallback(cls, handler_to_del):
        """
            Remove a fallback handler

            Args:
                handler_to_del: reference to handler
        """
        success = False
        for priority, handler in cls.fallback_handlers.items():
            if handler == handler_to_del:
                del cls.fallback_handlers[priority]
                success = True
        if not success:
            logger.warn('Could not remove fallback!')

        success = False
        for folder in cls.folders.keys():
            handler = cls.folders[folder]
            if handler == handler_to_del:
                del cls.folders[folder]
                success = True
        if not success:
            logger.warn('Could not remove ordered fallback!')

    def remove_instance_handlers(self):
        """
            Remove all fallback handlers registered by the fallback skill.
        """
        while len(self.instance_fallback_handlers):
            handler = self.instance_fallback_handlers.pop()
            self.remove_fallback(handler)

    def shutdown(self):
        """
            Remove all registered handlers and perform skill shutdown.
        """
        self.remove_instance_handlers()
        super(FallbackSkill, self).shutdown()
