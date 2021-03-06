"""
Copyright (c) 2015-2016 Nicholas H.Tollervey and others (see the AUTHORS file).

Based upon work done for Puppy IDE by Dan Pope, Nicholas Tollervey and Damien
George.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import os.path
import sys
import io
import re
import json
import logging
import tempfile
import webbrowser
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtSerialPort import QSerialPortInfo
from pyflakes.api import check
from pycodestyle import StyleGuide, Checker
from mu.contrib import uflash, appdirs, microfs
from mu import __version__
import time


#: USB product ID.
MICROBIT_PID = 516
#: USB vendor ID.
MICROBIT_VID = 3368
#: The user's home directory.
HOME_DIRECTORY = os.path.expanduser('~')
#: The default directory for Python scripts.
PYTHON_DIRECTORY = os.path.join(HOME_DIRECTORY, 'python')
#: The default directory for application data.
DATA_DIR = appdirs.user_data_dir('mu', 'python')
#: The default directory for application logs.
LOG_DIR = appdirs.user_log_dir('mu', 'python')
#: The path to the JSON file containing application settings.
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
#: The path to the log file for the application.
LOG_FILE = os.path.join(LOG_DIR, 'mu.log')
#: Regex to match pycodestyle (PEP8) output.
STYLE_REGEX = re.compile(r'.*:(\d+):(\d+):\s+(.*)')
#: Regex to match flake8 output.
FLAKE_REGEX = re.compile(r'.*:(\d+):\s+(.*)')


logger = logging.getLogger(__name__)


def find_upython_device():
    """
    TODO - allow option to select which serial port to use.
    For now, this just returns the first serial port found
    """
    available_ports = QSerialPortInfo.availablePorts()
    try:
        port = available_ports[0]
        logger.info('Using port {}'.format(port.portName()))
        return port.portName()
    except IndexError:
        return None

def check_flake(filename, code):
    """
    Given a filename and some code to be checked, uses the PyFlakesmodule to
    return a list of items describing issues of code quality. See:

    https://github.com/PyCQA/pyflakes
    """
    reporter = MuFlakeCodeReporter()
    check(code, filename, reporter)
    return reporter.log


def check_pycodestyle(code):
    """
    Given some code, uses the PyCodeStyle module (was PEP8) to return a list
    of items describing issues of coding style. See:

    https://pycodestyle.readthedocs.io/en/latest/intro.html
    """
    # PyCodeStyle reads input from files, so make a temporary file containing
    # the code.
    _, code_filename = tempfile.mkstemp()
    with open(code_filename, 'w') as code_file:
        code_file.write(code)
    # Configure which PEP8 rules to ignore.
    style = StyleGuide(parse_argv=False, config_file=False)
    checker = Checker(code_filename, options=style.options)
    # Re-route stdout to a temporary buffer to be parsed below.
    temp_out = io.StringIO()
    sys.stdout = temp_out
    # Check the code.
    checker.check_all()
    # Put stdout back and read the content of the buffer. Remove the temporary
    # file created at the start.
    sys.stdout = sys.__stdout__
    temp_out.seek(0)
    results = temp_out.read()
    temp_out.close()
    code_file.close()
    os.remove(code_filename)
    # Parse the output from the tool into a list of usefully structured data.
    style_feedback = []
    for result in results.split('\n'):
        matcher = STYLE_REGEX.match(result)
        if matcher:
            line_no, col, msg = matcher.groups()
            code, description = msg.split(' ', 1)
            if code == 'E303':
                description += ' above this line'
            style_feedback.append({
                'line_no': int(line_no),
                'column': int(col) - 1,
                'message': description.capitalize(),
                'code': code,
            })
    return style_feedback


class MuFlakeCodeReporter:
    """
    The class instantiates a reporter that creates structured data about
    code quality for Mu. Used by the PyFlakes module.
    """

    def __init__(self):
        """
        Set up the reporter object to be used to report PyFlake's results.
        """
        self.log = []

    def unexpectedError(self, filename, message):
        """
        Called if an unexpected error occured while trying to process the file
        called filename. The message parameter contains a description of the
        problem.
        """
        self.log.append({
            'line_no': 0,
            'filename': filename,
            'message': str(message)
        })

    def syntaxError(self, filename, message, line_no, column, source):
        """
        Records a syntax error in the file called filename.

        The message argument contains an explanation of the syntax error,
        line_no indicates the line where the syntax error occurred, column
        indicates the column on which the error occurred and source is the
        source code containing the syntax error.
        """
        msg = ('Syntax error. Python cannot understand this line. Check for '
               'missing characters!')
        self.log.append({
            'message': msg,
            'line_no': int(line_no),
            'column': column - 1,
            'source': source
        })

    def flake(self, message):
        """
        PyFlakes found something wrong with the code.
        """
        matcher = FLAKE_REGEX.match(str(message))
        if matcher:
            line_no, msg = matcher.groups()
            self.log.append({
                'line_no': int(line_no),
                'column': 0,
                'message': msg,
            })
        else:
            self.log.append({
                'line_no': 0,
                'column': 0,
                'message': str(message),
            })


class REPL:
    """
    Read, Evaluate, Print, Loop.

    Represents the REPL. Since the logic for the REPL is simply a USB/serial
    based widget this class only contains a reference to the associated port.
    """

    def __init__(self, port):
        if os.name == 'posix':
            # If we're on Linux or OSX reference the port is like this...
            self.port = "/dev/{}".format(port)
        elif os.name == 'nt':
            # On Windows simply return the port (e.g. COM0).
            self.port = port
        else:
            # No idea how to deal with other OS's so fail.
            raise NotImplementedError('OS not supported.')
        logger.info('Created new REPL object with port: {}'.format(self.port))


class Editor:
    """
    Application logic for the editor itself.
    """

    def __init__(self, view):
        logger.info('Setting up editor.')
        self._view = view
        self.repl = None
        self.fs = None
        self.theme = 'day'
        self.user_defined_microbit_path = None
        if not os.path.exists(PYTHON_DIRECTORY):
            logger.debug('Creating directory: {}'.format(PYTHON_DIRECTORY))
            os.makedirs(PYTHON_DIRECTORY)
        if not os.path.exists(DATA_DIR):
            logger.debug('Creating directory: {}'.format(DATA_DIR))
            os.makedirs(DATA_DIR)

    def restore_session(self):
        """
        Attempts to recreate the tab state from the last time the editor was
        run.
        """
        if os.path.exists(SETTINGS_FILE):
            logger.info('Restoring session from: {}'.format(SETTINGS_FILE))
            with open(SETTINGS_FILE) as f:
                old_session = json.load(f)
                logger.debug(old_session)
                if 'theme' in old_session:
                    self.theme = old_session['theme']
                if 'paths' in old_session:
                    for path in old_session['paths']:
                        try:
                            with open(path) as f:
                                text = f.read()
                        except FileNotFoundError:
                            pass
                        else:
                            self._view.add_tab(path, text)
        if not self._view.tab_count:
            py = 'from microbit import *\n\n# Write your code here :-)'
            self._view.add_tab(None, py)
        self._view.set_theme(self.theme)

    def flash(self):
        """
        Takes the currently active tab, compiles the Python script therein into
        a hex file and flashes it all onto the connected device.
        """
        logger.info('Flashing script')
        # Grab the Python script.
        tab = self._view.current_tab
        if tab is None:
            # There is no active text editor.
            return
        self.save()  # save current script to disk
        logger.debug('Python script file:')
        logger.debug(tab.path)
        microfs.put(microfs.get_serial(), tab.path)

    def add_fs(self):
        """
        If the REPL is not active, add the file system navigator to the UI.
        """
        if self.repl is None:
            if self.fs is None:
                try:
                    microfs.get_serial()
                    self._view.add_filesystem(home=PYTHON_DIRECTORY)
                    self.fs = True
                except IOError:
                    message = 'Could not find an attached BBC micro:bit.'
                    information = ("Please make sure the device is plugged "
                                   "into this computer.\n\nThe device must "
                                   "have MicroPython flashed onto it before "
                                   "the file system will work.\n\n"
                                   "Finally, press the device's reset button "
                                   "and wait a few seconds before trying "
                                   "again.")
                    self._view.show_message(message, information, parent = self._view)

    def remove_fs(self):
        """
        If the REPL is not active, remove the file system navigator from
        the UI.
        """
        if self.fs is None:
            raise RuntimeError("File system not running")
        self._view.remove_filesystem()
        self.fs = None

    def toggle_fs(self):
        """
        If the file system navigator is active enable it. Otherwise hide it.
        If the REPL is active, display a message.
        """
        if self.repl is not None:
            self.remove_repl()
        else:
            if self.fs is None:
                self.add_fs()
            else:
                self.remove_fs()

    def add_repl(self):
        """
        Detect a connected BBC micro:bit and if found, connect to the
        MicroPython REPL and display it to the user.
        """
        if self.fs:
            raise RuntimeError("File system already connected")
        logger.info('Starting REPL in UI.')
        if self.repl is not None:
            raise RuntimeError("REPL already running")
        mb_port = find_upython_device()
        if mb_port:
            try:
                self.repl = REPL(port=mb_port)
                self._view.add_repl(self.repl)
                logger.info('REPL on port: {}'.format(mb_port))
            except IOError as ex:
                logger.error(ex)
                self.repl = None
                information = ("Click the device's reset button, wait a few"
                               " seconds and then try again.")
                self._view.show_message(str(ex), information, parent = self._view)
            except Exception as ex:
                logger.error(ex)
        else:
            message = 'Could not find an attached BBC micro:bit.'
            information = ("Please make sure the device is plugged into this"
                           " computer.\n\nThe device must have MicroPython"
                           " flashed onto it before the REPL will work.\n\n"
                           "Finally, press the device's reset button and wait"
                           " a few seconds before trying again.")
            self._view.show_message(message, information, parent = self._view)

    def remove_repl(self):
        """
        If there's an active REPL, disconnect and hide it.
        """
        if self.repl is None:
            raise RuntimeError("REPL not running")
        self._view.remove_repl()
        self.repl = None

    def toggle_repl(self):
        """
        If the REPL is active, close it; otherwise open the REPL.
        """
        if self.fs is not None:
            self.remove_fs()
        else:
            if self.repl is None:
                self.add_repl()
            else:
                self.remove_repl()

    def toggle_theme(self):
        """
        Switches between themes (night or day).
        """
        if self.theme == 'day':
            self.theme = 'night'
        else:
            self.theme = 'day'
        logger.info('Toggle theme to: {}'.format(self.theme))
        self._view.set_theme(self.theme)

    def new(self):
        """
        Adds a new tab to the editor.
        """
        self._view.add_tab(None, '')

    def load(self):
        """
        Loads a Python file from the file system or extracts a Python sccript
        from a hex file.
        """
        path = self._view.get_load_path(PYTHON_DIRECTORY)
        logger.info('Loading script from: {}'.format(path))
        try:
            if path.endswith('.py'):
                # Open the file, read the textual content and set the name as
                # the path to the file.
                with open(path) as f:
                    text = f.read()
                name = path
            else:
                # Open the hex, extract the Python script therein and set the
                # name to None, thus forcing the user to work out what to name
                # the recovered script.
                with open(path) as f:
                    text = uflash.extract_script(f.read())
                name = None
        except FileNotFoundError:
            pass
        else:
            logger.debug(text)
            self._view.add_tab(name, text)

    def save(self):
        """
        Save the content of the currently active editor tab.
        """
        tab = self._view.current_tab
        if tab is None:
            # There is no active text editor so abort.
            return
        if tab.path is None:
            # Unsaved file.
            tab.path = self._view.get_save_path(PYTHON_DIRECTORY)
        if tab.path:
            # The user specified a path to a file.
            if not os.path.basename(tab.path).endswith('.py'):
                # No extension given, default to .py
                tab.path += '.py'
            with open(tab.path, 'w') as f:
                logger.info('Saving script to: {}'.format(tab.path))
                logger.debug(tab.text())
                f.write(tab.text())
            tab.setModified(False)
        else:
            # The user cancelled the filename selection.
            tab.path = None

    def zoom_in(self):
        """
        Make the editor's text bigger
        """
        self._view.zoom_in()

    def zoom_out(self):
        """
        Make the editor's text smaller.
        """
        self._view.zoom_out()

    def check_code(self):
        """
        Uses PyFlakes and PyCodeStyle to gather information about potential
        problems with the code in the current tab.
        """
        self._view.reset_annotations()
        tab = self._view.current_tab
        if tab is None:
            # There is no active text editor so abort.
            return
        filename = tab.path if tab.path else 'untitled'
        flake = check_flake(filename, tab.text())
        pep8 = check_pycodestyle(tab.text())
        # Consolidate the feedback into a dict, with line numbers as keys.
        feedback = {}
        for item in flake + pep8:
            line_no = int(item['line_no']) - 1  # zero based counting in Mu.
            if line_no in feedback:
                feedback[line_no].append(item)
            else:
                feedback[line_no] = [item, ]
        if feedback:
            logger.info(feedback)
            self._view.annotate_code(feedback)

    def show_help(self):
        """
        Display browser based help about Mu.
        """
        webbrowser.open_new('http://codewith.mu/help/{}'.format(__version__))

    def quit(self, *args, **kwargs):
        """
        Exit the application.
        """
        logger.info('Quitting')
        if self._view.modified:
            # Alert the user to handle unsaved work.
            msg = ('There is un-saved work, exiting the application will'
                   ' cause you to lose it.')
            result = self._view.show_confirmation(msg, parent = self._view)
            if result == QMessageBox.Cancel:
                if args and hasattr(args[0], 'ignore'):
                    # The function is handling an event, so ignore it.
                    args[0].ignore()
                return
        paths = []
        for widget in self._view.widgets:
            if widget.path:
                paths.append(widget.path)
        session = {
            'theme': self.theme,
            'paths': paths
        }
        logger.debug(session)
        with open(SETTINGS_FILE, 'w') as out:
            logger.debug('Saving session to: {}'.format(SETTINGS_FILE))
            json.dump(session, out, indent=2)
        sys.exit(0)
