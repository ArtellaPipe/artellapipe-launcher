import os
import re
import sys
import json
import time
import psutil
import shutil
import appdirs
import zipfile
import tarfile
import argparse
import platform
import requests
import traceback
import contextlib
import subprocess
import webbrowser
import logging.config
from pathlib2 import Path
from bs4 import BeautifulSoup
from backports import tempfile
from packaging.version import Version, InvalidVersion
try:
    from urlparse import urlparse
except Exception:
    from urllib.parse import urlparse
try:
    from urllib2 import Request, urlopen
except ImportError:
    from urllib.request import Request, urlopen

try:
    import PySide
    from PySide.QtCore import *
    from PySide.QtGui import *
except ImportError:
    from PySide2.QtCore import *
    from PySide2.QtWidgets import *
    from PySide2.QtGui import *


logging_name = '__logging__.ini'
logging_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), logging_name)
if not os.path.isfile(logging_path):
    logging_path = os.path.join(os.path.dirname(sys.executable), logging_name)
    if not os.path.isfile(logging_path):
        if hasattr(sys, '_MEIPASS'):
            logging_path = os.path.join(sys._MEIPASS, 'resources', logging_name)

logging.config.fileConfig(logging_path, disable_existing_loggers=False)
LOGGER = logging.getLogger('artellapipe-updater')

ARTELLA_NEXT_VERSION_FILE_NAME = 'version_to_run_next'


def is_windows():
    return sys.platform.startswith('win')


def is_mac():
    return sys.platform == 'darwin'


def is_linux():
    return 'linux' in sys.platform


class ArtellaSplash(QSplashScreen, object):
    def __init__(self, pixmap):

        self._offset = QPoint()

        super(ArtellaSplash, self).__init__(pixmap)

    def mousePressEvent(self, event):
        """
        Overrides base ArtellaDialog mousePressEvent function
        :param event: QMouseEvent
        """

        self._offset = event.pos()

    def mouseMoveEvent(self, event):
        """
        Overrides base ArtellaDialog mouseMoveEvent function
        :param event: QMouseEvent
        """

        x = event.globalX()
        y = event.globalY()
        x_w = self._offset.x()
        y_w = self._offset.y()
        self.move(x - x_w, y - y_w)


class ArtellaUpdaterException(Exception, object):
    def __init__(self, exc):
        if type(exc) in [str, unicode]:
            exc = Exception(exc)
        msg = '{} | {}'.format(exc, traceback.format_exc())
        LOGGER.exception(msg)
        traceback.print_exc()
        QMessageBox.critical(None, 'Error', msg)


class ArtellaUpdater(QWidget, object):
    def __init__(
            self, app, project_name, project_type, app_version, deployment_repository, documentation_url=None,
            deploy_tag=None, install_env_var=None, requirements_file_name=None, force_venv=False,
            splash_path=None, script_path=None, requirements_path=None, artellapipe_configs_path=None,
            dev=False, update_icon=False, parent=None):
        super(ArtellaUpdater, self).__init__(parent=parent)

        self._config_data = self._read_config()

        if app and update_icon:
            app.setWindowIcon(QIcon(self._get_resource(self._get_app_config('icon'))))

        self._dev = dev
        self._requirements_path = requirements_path if requirements_path else None
        self._artella_configs_path = artellapipe_configs_path if artellapipe_configs_path else None

        # We force development mode when we force a specific requirements file
        if self._requirements_path and os.path.isfile(self._requirements_path):
            self._dev = True

        self._project_name = self._get_app_config('name') or project_name
        self._project_type = self._get_app_config('type') or project_type
        self._app_version = self._get_app_config('version') or app_version
        self._repository = self._get_app_config('repository') or deployment_repository
        self._splash_path = self._get_resource(self._get_app_config('splash')) or splash_path

        self._force_venv = force_venv
        self._venv_info = dict()

        if self._project_name and not self._dev:
            for proc in psutil.process_iter():
                if proc.name().startswith(self._project_name) and proc.pid != psutil.Process().pid:
                    proc.kill()

        self._setup_logger()
        self._setup_config()

        self._setup_ui()
        QApplication.instance().processEvents()

        self._install_path = None
        self._selected_tag_index = None
        self._documentation_url = documentation_url if documentation_url else self._get_default_documentation_url()
        self._install_env_var = install_env_var if install_env_var else self._get_default_install_env_var()
        self._requirements_file_name = requirements_file_name if requirements_file_name else 'requirements.txt'
        self._all_tags = list()
        self._deploy_tag = deploy_tag if deploy_tag else self._get_deploy_tag()
        self._script_path = script_path if script_path and os.path.isfile(script_path) else self._get_script_path()
        self._artella_app = 'lifecycler' if self._project_type == 'indie' else 'artella'

        # If not valid tag is found we close the application
        if not self._deploy_tag:
            sys.exit()

        valid_load = self._load()
        if not valid_load:
            sys.exit()

    @property
    def project_name(self):
        return self._project_name

    @property
    def repository(self):
        return self._repository

    @property
    def install_env_var(self):
        return self._install_env_var

    def get_clean_name(self):
        """
        Return name of the project without spaces and lowercase
        :return: str
        """

        return self._project_name.replace(' ', '').lower()

    def get_current_os(self):
        """
        Return current OS the scrip is being executed on
        :return:
        """

        os_platform = platform.system()
        if os_platform == 'Windows':
            return 'Windows'
        elif os_platform == 'Darwin':
            return 'MacOS'
        elif os_platform == 'Linux':
            return 'Linux'
        else:
            raise Exception('No valid OS platform detected: {}!'.format(os_platform))

    def get_config_data(self):
        """
        Returns data in the configuration file
        :return: dict
        """

        data = dict()

        config_path = self._get_config_path()
        if not os.path.isfile(config_path):
            return data

        with open(config_path, 'r') as config_file:
            try:
                data = json.load(config_file)
            except Exception:
                data = dict()

        return data

    def is_python_installed(self):
        """
        Returns whether current system has Python installed or not
        :return: bool
        """

        process = self._run_subprocess(commands_list=['python', '-c', 'quit()'], shell=False)
        process.wait()

        return True if process.returncode == 0 else False

    def is_pip_installed(self):
        """
        Returns whether pip is installed or not
        :return: bool
        """

        process = self._run_subprocess(commands_list=['pip', '-V'])
        process.wait()

        return True if process.returncode == 0 else False

    def is_virtualenv_installed(self):
        """
        Returns whether virtualenv is intsalled or not
        :return: bool
        """

        try:
            process = self._run_subprocess(commands_list=['virtualenv', '--version'], shell=False)
            process.wait()
        except Exception:
            return False

        return True if process.returncode == 0 else False

    def _read_config(self):
        """
        Internal function that retrieves config data stored in executable
        :return: dict
        """

        data = {}
        config_file_name = 'config.json'
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config_file_name)
        if not os.path.isfile(config_path):
            config_path = os.path.join(os.path.dirname(sys.executable), 'resources', config_file_name)
            if not os.path.isfile(config_path):
                if hasattr(sys, '_MEIPASS'):
                    config_path = os.path.join(sys._MEIPASS, 'resources', config_file_name)

        if not os.path.isfile(config_path):
            return data

        try:
            with open(config_path) as config_file:
                data = json.load(config_file)
        except RuntimeError as exc:
            raise Exception(exc)

        return data

    def _get_app_config(self, config_name):
        """
        Returns configuration parameter stored in configuration, if exists
        :param config_name: str
        :return: str
        """

        if not self._config_data:
            return None

        return self._config_data.get(config_name, None)

    def _get_script_path(self):
        script_path = None
        config_file_name = 'launcher.py'
        script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), config_file_name)
        if not os.path.isfile(script_path):
            script_path = os.path.join(os.path.dirname(sys.executable), 'resources', config_file_name)
            if not os.path.isfile(script_path):
                if hasattr(sys, '_MEIPASS'):
                    script_path = os.path.join(sys._MEIPASS, 'resources', config_file_name)

        LOGGER.info('Launcher Script: "{}"'.format(script_path))

        return script_path

    def _get_resource(self, resource_name):
        resource_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', resource_name)
        if not os.path.isfile(resource_path):
            resource_path = os.path.join(os.path.dirname(sys.executable), 'resources', resource_name)
            if not os.path.isfile(resource_path):
                if hasattr(sys, '_MEIPASS'):
                    resource_path = os.path.join(sys._MEIPASS, 'resources', resource_name)

        LOGGER.info("Retrieving resource: {} >>> {}".format(resource_name, resource_path))

        return resource_path

    def _set_splash_text(self, new_text):
        self._progress_text.setText(new_text)
        QApplication.instance().processEvents()

    def _setup_ui(self):
        splash_pixmap = QPixmap(self._splash_path)
        self._splash = ArtellaSplash(splash_pixmap)
        self._splash.setWindowFlags(Qt.FramelessWindowHint)
        splash_layout = QVBoxLayout()
        splash_layout.setContentsMargins(5, 2, 5, 2)
        splash_layout.setSpacing(2)
        splash_layout.setAlignment(Qt.AlignBottom)
        self._splash.setLayout(splash_layout)

        label_style = """
        QLabel
        {
            background-color: rgba(100, 100, 100, 100);
            color: white;
            border-radius: 5px;
        }
        """

        self._version_lbl = QLabel('v0.0.0')
        self._version_lbl.setStyleSheet(label_style)
        version_font = self._version_lbl.font()
        version_font.setPointSize(10)
        self._version_lbl.setFont(version_font)

        self._artella_status_icon = QLabel()
        self._artella_status_icon.setPixmap(QPixmap(self._get_resource('artella_off.png')).scaled(QSize(30, 30)))

        install_path_icon = QLabel()
        install_path_icon.setPixmap(QPixmap(self._get_resource('disk.png')).scaled(QSize(25, 25)))
        self._install_path_lbl = QLabel('Install Path: ...')
        self._install_path_lbl.setStyleSheet(label_style)
        install_path_font = self._install_path_lbl.font()
        install_path_font.setPointSize(8)
        self._install_path_lbl.setFont(install_path_font)
        deploy_tag_icon = QLabel()
        deploy_tag_icon.setPixmap(QPixmap(self._get_resource('tag.png')).scaled(QSize(25, 25)))
        self._deploy_tag_combo = QComboBox()
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(5, 5, 5, 5)
        info_layout.setSpacing(10)

        buttons_style = """
        QPushButton:!hover
        {
            background-color: rgba(100, 100, 100, 100);
            color: white;
            border-radius: 5px;
        }
        QPushButton:hover
        {
            background-color: rgba(50, 50, 50, 100);
            color: white;
            border-radius: 5px;
        }
        QPushButton:pressed
        {
            background-color: rgba(15, 15, 15, 100);
            color: white;
            border-radius: 5px;
        }
        """

        self._launch_btn = QPushButton('Launch')
        self._launch_btn.setStyleSheet(buttons_style)
        self._launch_btn.setFixedWidth(150)
        self._launch_btn.setFixedHeight(30)
        self._launch_btn.setIconSize(QSize(40, 40))
        self._launch_btn.setIcon(QPixmap(self._get_resource('play.png')))
        self._close_btn = QPushButton('')
        self._close_btn.setFlat(True)
        self._close_btn.setFixedSize(QSize(30, 30))
        self._close_btn.setIconSize(QSize(25, 25))
        self._close_btn.setIcon(QPixmap(self._get_resource('close.png')))
        self._open_install_folder_btn = QPushButton('Open Install Folder')
        self._open_install_folder_btn.setStyleSheet(buttons_style)
        self._open_install_folder_btn.setFixedWidth(150)
        self._open_install_folder_btn.setFixedHeight(30)
        self._open_install_folder_btn.setIconSize(QSize(25, 25))
        self._open_install_folder_btn.setIcon(QPixmap(self._get_resource('search_folder.png')))
        self._reinstall_btn = QPushButton('Reinstall')
        self._reinstall_btn.setStyleSheet(buttons_style)
        self._reinstall_btn.setFixedWidth(75)
        self._reinstall_btn.setFixedHeight(30)
        self._reinstall_btn.setIconSize(QSize(15, 15))
        self._reinstall_btn.setIcon(QPixmap(self._get_resource('reinstall.png')))
        self._uninstall_btn = QPushButton('Uninstall')
        self._uninstall_btn.setStyleSheet(buttons_style)
        self._uninstall_btn.setFixedWidth(75)
        self._uninstall_btn.setFixedHeight(30)
        self._uninstall_btn.setIconSize(QSize(20, 20))
        self._uninstall_btn.setIcon(QPixmap(self._get_resource('uninstall.png')))
        uninstall_reinstall_layout = QHBoxLayout()
        uninstall_reinstall_layout.setSpacing(2)
        uninstall_reinstall_layout.setContentsMargins(2, 2, 2, 2)
        uninstall_reinstall_layout.addWidget(self._reinstall_btn)
        uninstall_reinstall_layout.addWidget(self._uninstall_btn)
        self._buttons_layout = QVBoxLayout()
        self._buttons_layout.setContentsMargins(5, 5, 5, 5)
        self._buttons_layout.setSpacing(2)
        self._buttons_layout.addWidget(self._launch_btn)
        self._buttons_layout.addWidget(self._open_install_folder_btn)
        self._buttons_layout.addLayout(uninstall_reinstall_layout)
        self._info_tag_btn = QPushButton()
        self._info_tag_btn.setFlat(True)
        self._info_tag_btn.setFixedSize(QSize(25, 25))
        self._info_tag_btn.setIconSize(QSize(18, 18))
        info_icon = QIcon()
        info_icon.addPixmap(QPixmap(self._get_resource('info.png')).scaled(QSize(25, 25)))
        self._info_tag_btn.setIcon(info_icon)
        self._refresh_tag_btn = QPushButton()
        self._refresh_tag_btn.setFlat(True)
        self._refresh_tag_btn.setFixedSize(QSize(25, 25))
        self._refresh_tag_btn.setIconSize(QSize(18, 18))
        refresh_icon = QIcon()
        refresh_icon.addPixmap(QPixmap(self._get_resource('refresh.png')).scaled(QSize(25, 25)))
        self._refresh_tag_btn.setIcon(refresh_icon)

        self._progress_text = QLabel('Setting {} ...'.format(self._project_name.title()))
        self._progress_text.setAlignment(Qt.AlignCenter)
        self._progress_text.setStyleSheet("QLabel { background-color : rgba(0, 0, 0, 180); color : white; }")
        font = self._progress_text.font()
        font.setPointSize(10)
        self._progress_text.setFont(font)

        second_layout = QHBoxLayout()
        second_layout.setContentsMargins(5, 5, 5, 5)
        second_layout.setSpacing(5)
        second_layout.addItem(QSpacerItem(10, 0, QSizePolicy.Expanding, QSizePolicy.Preferred))
        second_layout.addLayout(self._buttons_layout)
        second_layout.addItem(QSpacerItem(10, 0, QSizePolicy.Expanding, QSizePolicy.Preferred))

        splash_layout.addLayout(second_layout)
        splash_layout.addWidget(self._progress_text)

        self._artella_status_icon.setParent(self._splash)
        self._version_lbl.setParent(self._splash)
        self._close_btn.setParent(self._splash)
        install_path_icon.setParent(self._splash)
        self._install_path_lbl.setParent(self._splash)
        deploy_tag_icon.setParent(self._splash)
        self._deploy_tag_combo.setParent(self._splash)
        self._info_tag_btn.setParent(self._splash)
        self._refresh_tag_btn.setParent(self._splash)

        self._artella_status_icon.setFixedSize(QSize(45, 45))
        self._version_lbl.setFixedSize(50, 20)
        install_path_icon.setFixedSize(QSize(35, 35))
        self._install_path_lbl.setFixedSize(QSize(200, 20))
        deploy_tag_icon.setFixedSize(QSize(35, 35))
        self._deploy_tag_combo.setFixedSize(QSize(150, 20))

        height = 5
        self._version_lbl.move(10, self._splash.height() - 48)
        self._artella_status_icon.move(5, height)
        height += self._artella_status_icon.height() - 5
        install_path_icon.move(5, height)
        self._install_path_lbl.move(install_path_icon.width(), height + self._install_path_lbl.height() / 2 - 5)
        height += install_path_icon.height() - 5
        deploy_tag_icon.move(5, height)
        height = height + self._deploy_tag_combo.height() / 2 - 5
        self._deploy_tag_combo.move(deploy_tag_icon.width(), height)
        self._info_tag_btn.move(self._deploy_tag_combo.width() + self._info_tag_btn.width() + 10, height - 2)
        if not self._dev:
            self._refresh_tag_btn.move(self._deploy_tag_combo.width() + self._refresh_tag_btn.width() + 10, height - 2)
        else:
            self._refresh_tag_btn.move(
                self._deploy_tag_combo.width() + self._refresh_tag_btn.width() + self._info_tag_btn.width() + 10,
                height - 2)
        self._close_btn.move(self._splash.width() - self._close_btn.width() - 5, 0)

        self._deploy_tag_combo.setFocusPolicy(Qt.NoFocus)

        combo_width = 5
        if self._dev:
            self._deploy_tag_combo.setEnabled(False)
            combo_width = 0

        self._deploy_tag_combo.setStyleSheet("""
        QComboBox:!editable
        {
            background-color: rgba(100, 100, 100, 100);
            color: white;
            border-radius: 5px;
            padding: 1px 0px 1px 3px;
        }
        QComboBox::drop-down:!editable
        {
            background: rgba(50, 50, 50, 100);
            border-top-right-radius: 5px;
            border-bottom-right-radius: 5px;
            image: none;
            width: %dpx;
        }
        """ % combo_width)

        self._close_btn.setVisible(False)
        self._launch_btn.setVisible(False)
        self._open_install_folder_btn.setVisible(False)
        self._uninstall_btn.setVisible(False)
        self._reinstall_btn.setVisible(False)
        self._info_tag_btn.setVisible(False)
        self._refresh_tag_btn.setVisible(False)

        self._deploy_tag_combo.currentIndexChanged.connect(self._on_selected_tag)
        self._close_btn.clicked.connect(sys.exit)
        self._open_install_folder_btn.clicked.connect(self._on_open_installation_folder)
        self._launch_btn.clicked.connect(self.launch)
        self._reinstall_btn.clicked.connect(self._on_reinstall)
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        self._info_tag_btn.clicked.connect(self._on_open_tag_info)
        self._refresh_tag_btn.clicked.connect(self._on_refresh_tag)

        self._splash.show()
        self._splash.raise_()

    def _open_folder(self, path=None):
        """
        Opens a folder in the explorer in a independent platform way
        If not path is passed the current directory will be opened
        :param path: str, folder path to open
        """

        if path is None:
            path = os.path.curdir
        if sys.platform == 'darwin':
            self._check_call(commands_list=['open', '--', path])
        elif sys.platform == 'linux2':
            self._run_subprocess(commands_list=['xdg-open', path])
        elif sys.platform is 'windows' or 'win32' or 'win64':
            new_path = path.replace('/', '\\')
            try:
                self._check_call(commands_list=['explorer', new_path], shell=False)
            except Exception:
                pass

    def _clean_folder(self, folder):
        """
        Internal function that removes all the contents in the given folder
        :param folder: str
        """

        if not folder or not os.path.isdir(folder):
            LOGGER.warning('Impossible to remove "{}"'.format(folder))
            return

        for the_file in os.listdir(folder):
            file_path = os.path.join(folder, the_file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(e)

    def _setup_environment(self, clean=False):

        if not self._install_path:
            self._show_error('Impossible to setup virtual environment because install path is not defined!')
            return False

        if self._dev and not hasattr(sys, 'real_prefix'):
            self._show_error('Current Python"{}" is not installed in a virtual environment!'.format(
                os.path.dirname(sys.executable)))
            return False

        LOGGER.info("Setting Virtual Environment")
        venv_path = self._get_venv_folder_path()

        orig_force_env = self._force_venv
        if clean and os.path.isdir(venv_path):
            self._close_processes()
            self._clean_folder(venv_path)
            self._force_venv = True
        if self._force_venv or not os.path.isdir(venv_path):
            self._close_processes()
            self._create_venv(force=True)
        self._force_venv = orig_force_env

        root_path = os.path.dirname(venv_path)

        if is_windows():
            venv_scripts = os.path.join(venv_path, 'Scripts')
            venv_python = os.path.join(venv_scripts, 'python.exe')
            pip_exe = os.path.join(venv_scripts, 'pip.exe')
        elif is_mac():
            venv_scripts = os.path.join(venv_path, 'bin')
            venv_python = os.path.join(venv_scripts, 'python')
            pip_exe = os.path.join(venv_scripts, 'pip')

        venv_info = dict()
        venv_info['root_path'] = root_path
        venv_info['venv_folder'] = venv_path
        venv_info['venv_scripts'] = venv_scripts
        venv_info['venv_python'] = venv_python
        venv_info['pip_exe'] = pip_exe

        self._venv_info = venv_info

        LOGGER.info("Virtual Environment Info: {}".format(venv_info))

        # TODO: Check that all info contained in venv_info is valid

        return True

    def _close_processes(self):
        """
        Internal function that closes all opened Python processes but the current one
        """

        for proc in psutil.process_iter():
            if (proc.name().startswith('python') or proc.name().startswith(self._project_name)) \
                    and proc.pid != psutil.Process().pid:
                LOGGER.debug('Killing Python process: {}'.format(proc.name()))
                proc.kill()

    def _get_app_name(self):
        """
        Returns name of the app
        :return: str
        """

        return '{}_app'.format(self.get_clean_name())

    def _get_app_folder(self):
        """
        Returns folder where app data is located
        :return: str
        """

        logger_name = self._get_app_name()
        logger_path = os.path.dirname(appdirs.user_data_dir(logger_name))
        if not os.path.isdir(logger_path):
            os.makedirs(logger_path)

        if not os.path.isdir(logger_path):
            QMessageBox.critical(
                self,
                'Impossible to retrieve app data folder',
                'Impossible to retrieve app data folder.\n\n'
                'Please contact TD.'
            )
            return

        return logger_path

    def _check_setup(self):
        """
        Internal function that checks if environment is properly configured
        """

        self._set_splash_text('Checking if Python is installed ...')

        if not self.is_python_installed():
            LOGGER.warning('No Python Installation found!')
            QMessageBox.warning(
                self,
                'No Python Installation found in {}'.format(self.get_current_os()),
                'No valid Python installation found in your computer.\n\n'
                'Please follow instructions in {0} Documentation to install Python in your computer\n\n'
                'Click "Ok" to open {0} Documentation in your web browser'.format(self._project_name)
            )
            webbrowser.open(self._get_default_documentation_url())
            return False

        self._set_splash_text('Checking if pip is installed ...')

        if not self.is_pip_installed():
            LOGGER.warning('No pip Installation found!')
            QMessageBox.warning(
                self,
                'No pip Installation found in {}'.format(self.get_current_os()),
                'No valid pip installation found in your computer.\n\n'
                'Please follow instructions in {0} Documentation to install Python in your computer\n\n'
                'Click "Ok" to open {0} Documentation in your web browser'.format(self._project_name)
            )
            webbrowser.open(self._get_default_documentation_url())
            return False

        self._set_splash_text('Checking if virtualenv is installed ...')

        if not self.is_virtualenv_installed():
            LOGGER.warning('No virtualenv Installation found!')
            LOGGER.info('Installing virtualenv ...')
            process = self._run_subprocess(commands_list=['pip', 'install', 'virtualenv'])
            process.wait()
            if not self.is_virtualenv_installed():
                LOGGER.warning('Impossible to install virtualenv using pip.')
                QMessageBox.warning(
                    self,
                    'Impossible to install virtualenv in {}'.format(self.get_current_os()),
                    'Was not possible to install virtualenv in your computer.\n\n'
                    'Please contact your project TD.'
                )
                return False
            LOGGER.info('virtualenv installed successfully!')

        return True

    def _init_tags_combo(self):
        all_releases = self._get_all_releases()
        try:
            self._deploy_tag_combo.blockSignals(True)
            for release in all_releases:
                self._deploy_tag_combo.addItem(release)
        finally:
            if self._deploy_tag:
                deploy_tag_index = [i for i in range(self._deploy_tag_combo.count())
                                    if self._deploy_tag_combo.itemText(i) == self._deploy_tag]
                if deploy_tag_index:
                    self._selected_tag_index = deploy_tag_index[0]
                    self._deploy_tag_combo.setCurrentIndex(self._selected_tag_index)

            if not self._selected_tag_index:
                self._selected_tag_index = self._deploy_tag_combo.currentIndex()
            self._deploy_tag_combo.blockSignals(False)

    def _load(self, clean=False):
        """
        Internal function that initializes Artella App
        """

        valid_check = self._check_setup()
        if not valid_check:
            return False

        install_path = self._set_installation_path()
        if not install_path:
            return False

        self._version_lbl.setText(str('v{}'.format(self._app_version)))
        self._install_path_lbl.setText(install_path)
        self._install_path_lbl.setToolTip(install_path)

        self._init_tags_combo()

        valid_venv = self._setup_environment(clean=clean)
        if not valid_venv:
            return False
        if not self._venv_info:
            LOGGER.warning('No Virtual Environment info retrieved ...')
            return False
        valid_install = self._setup_deployment()
        if not valid_install:
            return False
        valid_artella = self._setup_artella()
        if not valid_artella:
            self._artella_status_icon.setPixmap(QPixmap(self._get_resource('artella_error.png')).scaled(QSize(30, 30)))
            self._artella_status_icon.setToolTip('Error while connecting to Artella server!')
            return False
        else:
            self._artella_status_icon.setPixmap(QPixmap(self._get_resource('artella_ok.png')).scaled(QSize(30, 30)))
            self._artella_status_icon.setToolTip('Artella Connected!')

        self._set_splash_text('{} Launcher is ready to lunch!'.format(self._project_name))

        self._close_btn.setVisible(True)
        self._info_tag_btn.setVisible(True)

        # We check that stored config path exits
        stored_path = self._get_app_config(self._install_env_var)
        if stored_path and not os.path.isdir(stored_path):
            self._set_config(self._install_env_var, '')

        path_install = self._get_installation_path()
        is_installed = path_install and os.path.isdir(path_install)
        if is_installed:
            self._launch_btn.setVisible(True)
            if not self._dev:
                self._open_install_folder_btn.setVisible(True)
                self._reinstall_btn.setVisible(True)
                self._uninstall_btn.setVisible(True)
            else:
                self._refresh_tag_btn.setVisible(True)
        else:
            QMessageBox.warning(
                self,
                'Was not possible to install {} environment.'.format(self._project_name),
                'Was not possible to install {} environment.\n\n'
                'Relaunch the app. If the problem persists, please contact your project TD'.format(
                    self._project_name))

        return True

    def launch(self):

        if not self._venv_info:
            LOGGER.warning(
                'Impossible to launch {} Launcher because Virtual Environment Setup is not valid!'.format(
                    self._project_name))
            return False

        py_exe = self._venv_info['venv_python']
        if not self._script_path or not os.path.isfile(self._script_path):
            raise Exception('Impossible to find launcher script!')

        LOGGER.info('Executing {} Launcher ...'.format(self._project_name))

        paths_to_register = self._get_paths_to_register()

        process_cmd = '"{}" "{}" --project-name {} --install-path "{}" --paths-to-register "{}" --tag "{}"'.format(
            py_exe, self._script_path, self.get_clean_name(), self._install_path, '"{0}"'.format(
                ' '.join(paths_to_register)), self._deploy_tag)
        if self._artella_configs_path:
            process_cmd += ' --artella-configs-path "{}"'.format(self._artella_configs_path)

        if self._dev:
            process_cmd += ' --dev'
        process = self._run_subprocess(command=process_cmd, close_fds=True)

        self._splash.close()

        # if not self._dev:
        # time.sleep(3)
        # QApplication.instance().quit()
        # sys.exit()

    def _check_installation_path(self, install_path):
        """
        Returns whether or not given path is valid
        :param install_path: str
        :return: bool
        """

        if not install_path or not os.path.isdir(install_path):
            return False

        return True

    def _set_installation_path(self):
        """
        Returns installation path is if it already set by user; Otherwise a dialog to select it will appear
        :return: str
        """

        path_updated = False
        install_path = self._get_installation_path()

        # Remove older installations
        self._set_splash_text('Searching old installation ...')
        old_installation = False
        if os.path.isdir(install_path):
            for d in os.listdir(install_path):
                if d == self.get_clean_name():
                    old_dir = os.path.join(install_path, d)
                    content = os.listdir(old_dir)
                    if is_windows():
                        if 'Include' not in content or 'Lib' not in content or 'Scripts' not in content:
                            old_installation = True
                            break
                    elif is_mac():
                        if 'include' not in content or 'lib' not in content or 'bin' not in content:
                            old_installation = True
                            break

        if old_installation:
            LOGGER.info("Old installation found. Removing ...")
            self._set_config(self.install_env_var, '')
            self._set_splash_text('Removing old installation ...')
            res = QMessageBox.question(
                self._splash, 'Old installation found',
                'All the contents in the following folder wil be removed: \n\t{}\n\nDo you want to continue?'.format(
                    install_path), QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No)
            if res == QMessageBox.Yes:
                shutil.rmtree(install_path)
            QMessageBox.information(
                self._splash,
                'Relaunch the tool',
                'Next time you launch the tool you will need to select a new installation path')
            return False

        if not install_path or not os.path.isdir(install_path):
            self._set_splash_text('Select {} installation folder ...'.format(self._project_name))
            install_path = QFileDialog.getExistingDirectory(
                None, 'Select Installation Path for {}'.format(self._project_name))
            if not install_path:
                LOGGER.info('Installation cancelled by user')
                QMessageBox.information(
                    self._splash,
                    'Installation cancelled',
                    'Installation cancelled by user')
                return False
            if not os.path.isdir(install_path):
                LOGGER.info('Selected Path does not exist!')
                QMessageBox.information(
                    self,
                    'Selected Path does not exist',
                    'Selected Path: "{}" does not exist. '
                    'Installation cancelled!'.format(install_path))
                return False
            path_updated = True

        self._set_splash_text('Checking if Install Path is valid ...')
        LOGGER.info('>>>>>> Checking Install Path: {}'.format(install_path))
        valid_path = self._check_installation_path(install_path)
        if not valid_path:
            LOGGER.warning('Selected Install Path is not valid!')
            return

        if path_updated:
            self._set_splash_text('Registering new install path ...')
            valid_update_config = self._set_config(self.install_env_var, install_path)
            if not valid_update_config:
                return

        self._set_splash_text('Install Path: {}'.format(install_path))
        LOGGER.info('>>>>>> Install Path: {}'.format(install_path))

        self._install_path = install_path

        return install_path

    def _setup_logger(self):
        """
        Setup logger used by the app
        """

        logger_name = self._get_app_name()
        logger_path = self._get_app_folder()
        logger_file = os.path.normpath(os.path.join(logger_path, '{}.log'.format(logger_name)))

        fh = logging.FileHandler(logger_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        LOGGER.addHandler(fh)

        LOGGER.info('\n')
        LOGGER.info('{} Logger: "{}"'.format(self._project_name, logger_file))
        LOGGER.info("=" * 150)
        LOGGER.debug('Starting {} App'.format(self._project_name))
        LOGGER.info("=" * 150)

    def _clean_old_config(self):
        """
        Function used to clean
        """

        current_os = self.get_current_os()

        if current_os == 'Windows':
            config_directory = Path(os.getenv('APPDATA') or '~')
        elif current_os == 'MacOS':
            config_directory = Path('~', 'Library', 'Preferences')
        else:
            config_directory = Path(os.getenv('XDG_CONFIG_HOME') or '~/.config')

        old_config_path = config_directory.joinpath(Path('{}/.config'.format(self.get_clean_name())))
        if old_config_path.exists():
            LOGGER.info('Old Configuration found in "{}". Removing ...'.format(str(old_config_path)))
            try:
                os.remove(str(old_config_path))
            except RuntimeError as exc:
                msg = 'Impossible to remove old configuration file: {} | {}'.format(exc, traceback.format_exc())
                self._show_error(msg)
                return False
            LOGGER.info('Old Configuration file removed successfully!')

        return True

    def _setup_config(self):
        """
        Internal function that creates an empty configuration file if it is not already created
        :return: str
        """

        self._clean_old_config()

        config_file = self._get_config_path()
        if not os.path.isfile(config_file):
            LOGGER.info('Creating {} App Configuration File: {}'.format(self._project_name, config_file))
            with open(config_file, 'w') as cfg:
                json.dump({}, cfg)
            if not os.path.isfile(config_file):
                QMessageBox.critical(
                    self,
                    'Impossible to create configuration file',
                    'Impossible to create configuration file.\n\n'
                    'Please contact TD.'
                )
                return

        LOGGER.info('Configuration File found: "{}"'.format(config_file))

        return config_file

    def _get_installation_path(self):
        """
        Returns current installation path stored in config file
        :return: str
        """

        if self._dev:
            if hasattr(sys, 'real_prefix'):
                install_path = os.path.dirname(os.path.dirname(sys.executable))
            else:
                install_path = os.path.dirname(sys.executable)
        else:
            config_data = self.get_config_data()
            install_path = config_data.get(self.install_env_var, '')

        return install_path

    def _get_default_documentation_url(self):
        """
        Internal function that returns a default value for the documentation URL taking into account the project name
        :return: str
        """

        return 'https://{}-short-film.github.io/{}-docs/pipeline/'.format(self._project_name, self.get_clean_name())

    def _get_deploy_repository_url(self, release=False):
        """
        Internal function that returns a default path for the deploy repository taking int account the project name
        :param release: bool, Whether to retrieve releases path or the package to download
        :return: str
        """

        if release:
            return 'https://github.com/{}/releases'.format(self._repository)
        else:
            return 'https://github.com/{}/archive/{}.tar.gz'.format(self._repository, self._deploy_tag)

    def _sanitize_github_version(self, version):
        """extract what appears to be the version information"""
        s = re.search(r'([0-9]+([.][0-9]+)+(rc[0-9]?)?)', version)
        if s:
            return s.group(1)
        else:
            return version.strip()

    def _get_all_releases(self):
        """
        Internal function that returns a list with all released versions of the deploy repository taking into account
        the project name
        :return: list(str)
        """

        if self._dev:
            return ['DEV']

        all_versions = list()

        repository = self._get_deploy_repository_url(release=True)
        if not repository:
            msg = '> Project {} GitHub repository is not valid! {}'.format(self._project_name.title(), repository)
            self._show_error(msg)
            return None

        if repository.startswith('https://github.com/'):
            repository = "/".join(repository.split('/')[3:5])

        release_url = "https://github.com/{}/releases".format(repository)
        response = requests.get(release_url, headers={'Connection': 'close'})
        html = response.text
        LOGGER.debug('Parsing HTML of {} GitHub release page ...'.format(self._project_name.title()))

        soup = BeautifulSoup(html, 'lxml')

        releases = soup.findAll(class_='release-entry')
        for release in releases:
            release_a = release.find("a")
            if not release_a:
                continue
            the_version = release_a.text
            if 'Latest' in the_version:
                label_latest = release.find(class_='label-latest', recursive=False)
                if label_latest:
                    the_version = release.find(class_='css-truncate-target').text
                    the_version = self._sanitize_github_version(the_version)
            else:
                the_version = self._sanitize_github_version(the_version)

            if the_version not in all_versions:
                all_versions.append(the_version)

        return all_versions

    def _get_deploy_tag(self):
        """
        Internal function that returns the current tag that should be used for deployment
        :return: str
        """

        if self._dev:
            return 'DEV'

        config_data = self.get_config_data()
        deploy_tag = config_data.get('tag', '')
        latest_deploy_tag = self._get_latest_deploy_tag()
        if not latest_deploy_tag:
            return None

        if not deploy_tag:
            deploy_tag = latest_deploy_tag

        deploy_tag_v = Version(deploy_tag)
        latest_tag_v = Version(latest_deploy_tag)
        if latest_tag_v > deploy_tag_v:
            res = QMessageBox.question(
                self._splash, 'Newer version found: {}'.format(latest_deploy_tag),
                'Current Version: {}\nNew Version: {}\n\nDo you want to install new version?'.format(
                    deploy_tag, latest_deploy_tag), QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No)
            if res == QMessageBox.Yes:
                self._set_config('tag', latest_deploy_tag)
                deploy_tag = latest_deploy_tag

        LOGGER.info("Deploy Tag to use: {}".format(deploy_tag))

        return deploy_tag

    def _get_latest_deploy_tag(self, sniff=True, validate=True, format='version', pre=False):
        """
        Returns last deployed version of the given repository in GitHub
        :return: str
        """

        if self._dev:
            return 'DEV'

        self._all_tags = list()

        version = None
        description = None
        data = None

        repository = self._get_deploy_repository_url(release=True)
        if not repository:
            msg = '> Project {} GitHub repository is not valid! {}'.format(self._project_name.title(), repository)
            self._show_error(msg)
            return None

        if repository.startswith('https://github.com/'):
            repository = "/".join(repository.split('/')[3:5])

        if sniff:
            release_url = "https://github.com/{}/releases".format(repository)
            response = requests.get(release_url, headers={'Connection': 'close'})
            html = response.text
            LOGGER.debug('Parsing HTML of {} GitHub release page ...'.format(self._project_name.title()))

            soup = BeautifulSoup(html, 'lxml')

            r = soup.find(class_='release-entry')
            while r:
                break_out = False
                if 'release-timeline-tags' in r['class']:
                    for release in r.find_all(class_='release-entry', recursive=False):
                        release_a = release.find("a")
                        if not release_a:
                            continue
                        the_version = release_a.text
                        the_version = self._sanitize_github_version(the_version)
                        if validate:
                            try:
                                LOGGER.debug("Trying version {}.".format(the_version))
                                v = Version(the_version)
                                if not v.is_prerelease or pre:
                                    LOGGER.debug("Good version {}.".format(the_version))
                                    version = the_version
                                    break_out = True
                                    break
                            except InvalidVersion:
                                # move on to next thing to parse it
                                msg = 'Encountered invalid version {}.'.format(the_version)
                                self._show_error(msg)
                        else:
                            version = the_version
                            break
                    if break_out:
                        break
                else:
                    LOGGER.debug("Inside formal release")
                    # formal release
                    if pre:
                        label_latest = r.find(class_='label-prerelease', recursive=False)
                    else:
                        label_latest = r.find(class_='label-latest', recursive=False)
                    if label_latest:
                        the_version = r.find(class_='css-truncate-target').text
                        the_version = self._sanitize_github_version(the_version)
                        # check if version is ok and not a prerelease; move on to next tag otherwise
                        if validate:
                            try:
                                v = Version(the_version)
                                if not v.is_prerelease or pre:
                                    version = the_version
                                    # extra info for json output
                                    if format == 'json':
                                        description = r.find(class_='markdown-body')
                                        if not description:
                                            description = r.find(class_='commit-desc')
                                            if description:
                                                description = description.text
                                    break
                                else:
                                    LOGGER.debug("Found a pre-release version: {}. Trying next.".format(the_version))
                            except InvalidVersion:
                                # move on to next thing to parse it
                                msg = 'Encountered invalid version {}.'.format(the_version)
                                self._show_error(msg)
                        else:
                            version = the_version
                            break

                r = r.find_next_sibling(class_='release-entry', recursive=False)

        if not version:
            msg = 'Impossible to retrieve {} lastest release version from GitHub!'.format(self._project_name.title())
            self._show_error(msg)
            return None

        if validate:
            try:
                Version(version)
            except InvalidVersion:
                msg = 'Got invalid version: {}'.format(version)
                self._show_error(msg)
                return None

        # return the release if we've reached far enough:
        if format == 'version':
            return version
        elif format == 'json':
            if not data:
                data = {}
            if description:
                description = description.strip()
            data['version'] = version
            data['description'] = description
            return json.dumps(data)

    def _get_default_install_env_var(self):
        """
        Internal function that returns a default env var
        :return: str
        """

        return '{}_install'.format(self.get_clean_name())

    def _get_config_path(self):
        """
        Internal function that returns path where configuration file is located
        :return: str
        """

        config_name = self._get_app_name()
        config_path = self._get_app_folder()
        config_file = os.path.normpath(os.path.join(config_path, '{}.cfg'.format(config_name)))

        return config_file

    def _set_config(self, config_name, config_value):
        """
        Sets configuration and updates the file
        :param config_name: str
        :param config_value: object
        """

        config_path = self._get_config_path()
        if not os.path.isfile(config_path):
            LOGGER.warning(
                'Impossible to update configuration file because it does not exists: "{}"'.format(config_path))
            return False

        config_data = self.get_config_data()
        config_data[config_name] = config_value
        with open(config_path, 'w') as config_file:
            json.dump(config_data, config_file)

        return True

    def _create_venv(self, force=False):
        """
        Internal function that creates virtual environment
        :param force: bool
        :return: bool
        """

        venv_path = self._get_venv_folder_path()

        if self._check_venv_folder_exists() and not force:
            LOGGER.info('Virtual Environment already exists: "{}"'.format(venv_path))
            return True

        if force and self._check_venv_folder_exists() and os.path.isdir(venv_path):
            LOGGER.info('Forcing the removal of Virtual Environment folder: "{}"'.format(venv_path))
            self._set_splash_text('Removing already existing virtual environment ...')
            shutil.rmtree(venv_path)

        self._set_splash_text('Creating Virtual Environment: "{}"'.format(venv_path))
        process = self._run_subprocess(commands_list=['virtualenv', venv_path], shell=False)
        process.wait()

        return True if process.returncode == 0 else False

    def _get_venv_folder_path(self):
        """
        Returns path where virtual environment folder should be located
        :return: str
        """

        if not self._install_path:
            return

        if self._dev:
            return os.path.normpath(self._install_path)
        else:
            return os.path.normpath(os.path.join(self._install_path, self.get_clean_name()))

    def _get_paths_to_register(self):
        """
        Returns paths that will be registered in sys.path during DCC environment loading
        :return: list(str)
        """

        paths_to_register = [self._get_installation_path()]

        if self._dev:
            lib_site_folder = os.path.join(self._install_path, 'Lib', 'site-packages')
        else:
            lib_site_folder = os.path.join(self._install_path, self.get_clean_name(), 'Lib', 'site-packages')

        if os.path.isdir(lib_site_folder):
            paths_to_register.append(lib_site_folder)

        return paths_to_register

    def _check_venv_folder_exists(self):
        """
        Returns whether or not virtual environment folder for this project exists or not
        :return: bool
        """

        venv_path = self._get_default_install_env_var()
        if not venv_path:
            return False

        return os.path.isdir(venv_path)

    def _try_download_unizip_deployment_requirements(self, deployment_url, download_path, dirname):
        valid_download = self._download_file(deployment_url, download_path)
        if not valid_download:
            return False

        try:
            valid_unzip = self._unzip_file(filename=download_path, destination=dirname, remove_sub_folders=[])
        except Exception:
            valid_unzip = False
        if not valid_unzip:
            return False

        return True

    def _download_deployment_requirements(self, dirname):
        """
        Internal function that downloads the current deployment requirements
        """

        self._set_splash_text('Downloading {} Deployment Information ...'.format(self._project_name))
        deployment_url = self._get_deploy_repository_url()
        if not deployment_url:
            msg = 'Deployment URL not found!'
            self._show_error(msg)
            return False

        response = requests.get(deployment_url, headers={'Connection': 'close'})
        if response.status_code != 200:
            msg = 'Deployment URL is not valid: "{}"'.format(deployment_url)
            self._show_error(msg)
            return False

        repo_name = urlparse(deployment_url).path.rsplit("/", 1)[-1]
        download_path = os.path.join(dirname, repo_name)

        valid_status = False
        total_tries = 0
        self._set_splash_text('Downloading and Unzipping Deployment Data ...')
        while not valid_status:
            if total_tries > 10:
                break
            valid_status = self._try_download_unizip_deployment_requirements(deployment_url, download_path, dirname)
            total_tries += 1
            if not valid_status:
                LOGGER.warning('Retrying downloading and unzip deployment data: {}'.format(total_tries))
        if not valid_status:
            msg = 'Something went wrong during the download and unzipping of: {}'.format(deployment_url)
            self._show_error(msg)
            return False

        self._set_splash_text('Searching Requirements File: {}'.format(self._requirements_file_name))
        requirement_path = None
        for root, dirs, files in os.walk(dirname):
            for name in files:
                if name == self._requirements_file_name:
                    requirement_path = os.path.join(root, name)
                    break
        if not requirement_path:
            msg = 'No file named: {} found in deployment repository!'.format(self._requirements_file_name)
            self._show_error(msg)
            return False
        LOGGER.debug('Requirements File for Deployment "{}" found: "{}"'.format(deployment_url, requirement_path))
        self._requirements_path = requirement_path

        return True

    def _install_deployment_requirements(self):
        if not self._venv_info:
            self._show_error(
                'Impossible to install Deployment Requirements because Virtual Environment is not configured!')
            return False

        if not self._requirements_path or not os.path.isfile(self._requirements_path):
            self._show_error(
                'Impossible to install Deployment Requirements because file does not exists:\n\n"{}"'.format(
                    self._requirements_path)
            )
            return False

        pip_exe = self._venv_info.get('pip_exe', None)
        if not pip_exe or not os.path.isfile(pip_exe):
            self._show_error(
                'Impossible to install Deployment Requirements because pip not found installed in '
                'Virtual Environment:\n\n"{}"'.format(pip_exe)
            )
            return False

        self._set_splash_text('Installing {} Requirements. Please wait ...'.format(self._project_name))
        LOGGER.info('Installing Deployment Requirements with PIP: {}'.format(pip_exe))

        pip_cmd = '"{}" install --upgrade --no-cache -r "{}"'.format(pip_exe, self._requirements_path)
        LOGGER.info('Launching pip command: {}'.format(pip_cmd))

        try:
            if is_windows():
                start_time = time.time()
                LOGGER.info('\nPip install --> first try ...')
                process = self._run_subprocess(command=pip_cmd)
                output, error = process.communicate()
                LOGGER.info('Pip install --> first try ---> executed in {} seconds\n!'.format(time.time() - start_time))
                LOGGER.info(output)
                LOGGER.error(error)

                # We retry twice because sometimes pip fails when trying to install new packages
                start_time = time.time()
                LOGGER.info('\nPip install --> second try ...')
                process = self._run_subprocess(command=pip_cmd)
                output, error = process.communicate()
                LOGGER.info(output)
                LOGGER.error(error)
                LOGGER.info('Pip install --> first try ---> executed in {} seconds\n!'.format(time.time() - start_time))
                if error:
                    show_error = False
                    error_split = error.split('\n')
                    for error_str in error_split:
                        if not error_str or error_str.startswith(
                                ('DEPRECATION:', 'WARNING:', 'You should consider upgrading via')):
                            continue
                        else:
                            show_error = True
                            break
                    if show_error:
                        error_dlg = AppErrorDialog(error)
                        error_dlg.exec_()
                        return False

        except Exception as exc:
            raise ArtellaUpdaterException(exc)

        return True

    def _setup_deployment(self):
        if not self._venv_info:
            return False

        if self._dev:
            if self._install_path and self._requirements_path and os.path.isfile(self._requirements_path):
                valid_install = self._install_deployment_requirements()
                if not valid_install:
                    LOGGER.info("Error while installing requirements. Trying to uninstall ...")
                    res = QMessageBox.question(
                        self._splash,
                        'Impossible to install/update tools properly',
                        'Current tools installation is not valid.\n\nDo you want to clean current installation?.\n\n'
                        'If you press Yes, next time you launch the application, you will need to select a '
                        'new installation path and tools will be fully reinstalled.',
                        buttons=QMessageBox.Yes | QMessageBox.No)
                    if res == QMessageBox.Yes:
                        self._on_uninstall(force=True)
                    return False
            return True

        with tempfile.TemporaryDirectory() as temp_dirname:
            valid_download = self._download_deployment_requirements(temp_dirname)
            if not valid_download or not self._requirements_path or not os.path.isfile(self._requirements_path):
                return False
            valid_install = self._install_deployment_requirements()
            if not valid_install:
                LOGGER.info("Error while installing requirements. Trying to uninstall ...")
                res = QMessageBox.question(
                    self._splash,
                    'Impossible to install/update tools properly',
                    'Current tools installation is not valid.\n\nDo you want to clean current installation?.\n\n'
                    'If you press Yes, next time you launch the application, you will need to select a '
                    'new installation path and tools will be fully reinstalled.',
                    buttons=QMessageBox.Yes | QMessageBox.No)
                if res == QMessageBox.Yes:
                    self._on_uninstall(force=True)
                return False

        return True

    def _setup_artella(self):
        """
        Internal function that initializes Artella
        """

        self._set_splash_text('Updating Artella Paths ...')
        self._update_artella_paths()
        self._set_splash_text('Closing Artella App instances ...')
        # For now we do not check if Artella was closed or not
        self._close_all_artella_app_processes()
        self._set_splash_text('Launching Artella App ...')
        self._launch_artella_app()

        return True

    def _download_file(self, filename, destination):
        """
        Downloads given file into given target path
        :param filename: str
        :param destination: str
        :param console: ArtellaConsole
        :param updater: ArtellaUpdater
        :return: bool
        """

        def _chunk_report(bytes_so_far, total_size):
            """
            Function that updates progress bar with current chunk
            :param bytes_so_far: int
            :param total_size: int
            :param console: ArtellaConsole
            :param updater: ArtellaUpdater
            :return:
            """

            percent = float(bytes_so_far) / total_size
            percent = round(percent * 100, 2)
            msg = "Downloaded %d of %d bytes (%0.2f%%)" % (bytes_so_far, total_size, percent)
            self._set_splash_text(msg)
            LOGGER.info(msg)

        def _chunk_read(response, destination, chunk_size=8192, report_hook=None):
            """
            Function that reads a chunk of a dowlnoad operation
            :param response: str
            :param destination: str
            :param console: ArtellaLauncher
            :param chunk_size: int
            :param report_hook: fn
            :param updater: ArtellaUpdater
            :return: int
            """

            with open(destination, 'ab') as dst_file:
                rsp = response.info().getheader('Content-Length')
                if not rsp:
                    return
                total_size = rsp.strip()
                total_size = int(total_size)
                bytes_so_far = 0
                while 1:
                    chunk = response.read(chunk_size)
                    dst_file.write(chunk)
                    bytes_so_far += len(chunk)
                    if not chunk:
                        break
                    if report_hook:
                        report_hook(bytes_so_far=bytes_so_far, total_size=total_size)
            dst_file.close()
            return bytes_so_far

        LOGGER.info('Downloading file {} to temporary folder -> {}'.format(os.path.basename(filename), destination))
        try:
            dst_folder = os.path.dirname(destination)
            if not os.path.exists(dst_folder):
                LOGGER.info('Creating Download Folder: "{}"'.format(dst_folder))
                os.makedirs(dst_folder)

            hdr = {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.11 (KHTML, like Gecko) '
                              'Chrome/23.0.1271.64 Safari/537.11',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.3',
                'Accept-Encoding': 'none',
                'Accept-Language': 'en-US,en;q=0.8',
                'Connection': 'keep-alive'}
            req = Request(filename, headers=hdr)
            data = urlopen(req)
            _chunk_read(response=data, destination=destination, report_hook=_chunk_report)
        except Exception as exc:
            raise Exception(exc)

        if os.path.exists(destination):
            LOGGER.info('Files downloaded succesfully!')
            return True
        else:
            msg = 'Error when downloading files. Maybe server is down! Try it later'
            self._show_error(msg)
            return False

    def _unzip_file(self, filename, destination, remove_first=True, remove_sub_folders=None):
        """
        Unzips given file in given folder
        :param filename: str
        :param destination: str
        :param console: ArtellaConsole
        :param remove_first: bool
        :param remove_sub_folders: bool
        """

        LOGGER.info('Unzipping file {} to --> {}'.format(filename, destination))
        try:
            if remove_first and remove_sub_folders:
                LOGGER.info('Removing old installation ...')
                for sub_folder in remove_sub_folders:
                    p = os.path.join(destination, sub_folder)
                    LOGGER.info('\t{}'.format(p))
                    if os.path.exists(p):
                        shutil.rmtree(p)
            if not os.path.exists(destination):
                LOGGER.info('Creating destination folders ...')
                QApplication.instance().processEvents()
                os.makedirs(destination)

            if filename.endswith('.tar.gz'):
                zip_ref = tarfile.open(filename, 'r:gz')
            elif filename.endswith('.tar'):
                zip_ref = tarfile.open(filename, 'r:')
            else:
                zip_ref = zipfile.ZipFile(filename, 'r')
            zip_ref.extractall(destination)
            zip_ref.close()
            return True
        except Exception as exc:
            raise Exception(exc)

    def _get_artella_data_folder(self):
        """
        Returns last version Artella folder installation
        :return: str
        """

        if is_mac():
            artella_folder = os.path.join(os.path.expanduser('~/Library/Application Support/'), 'Artella')
        elif is_windows():
            if self._project_type == 'indie':
                artella_folder = os.path.join(os.getenv('PROGRAMDATA'), 'Artella')
            else:
                artella_folder = os.path.join(os.getenv('ProgramFiles(x86)'), 'Artella')
        else:
            return None

        if self._project_type == 'indie':
            version_file = os.path.join(artella_folder, ARTELLA_NEXT_VERSION_FILE_NAME)
            if os.path.isfile(version_file):
                with open(version_file) as f:
                    artella_app_version = f.readline()

                if artella_app_version is not None:
                    artella_folder = os.path.join(artella_folder, artella_app_version)
                else:
                    artella_folder = [
                        os.path.join(artella_folder, name) for name in os.listdir(artella_folder) if os.path.isdir(
                            os.path.join(artella_folder, name)) and name != 'ui']
                    if len(artella_folder) == 1:
                        artella_folder = artella_folder[0]
                    else:
                        LOGGER.info('Artella folder not found!')

        LOGGER.debug('ARTELLA FOLDER: {}'.format(artella_folder))
        if not os.path.exists(artella_folder):
            QMessageBox.information(
                self._splash,
                'Artella Folder not found!',
                'Artella App Folder {} does not exists! Make sure that Artella is installed in your computer!')

        return artella_folder

    def _update_artella_paths(self):
        """
        Updates system path to add artella paths if they are not already added
        :return:
        """

        # Artella update paths is only needed for Artella Indie projects
        if self._project_type != 'indie':
            return

        artella_folder = self._get_artella_data_folder()

        LOGGER.debug('Updating Artella paths from: {0}'.format(artella_folder))
        if artella_folder is not None and os.path.exists(artella_folder):
            for subdir, dirs, files in os.walk(artella_folder):
                if subdir not in sys.path:
                    LOGGER.debug('Adding Artella path: {0}'.format(subdir))
                    sys.path.append(subdir)

    def _close_all_artella_app_processes(self):
        """
        Closes all Artella app (lifecycler.exe) processes
        :return:
        """

        try:
            proc_name = self._artella_app
            if is_windows():
                proc_name = '{}.exe'.format(proc_name)
            for proc in psutil.process_iter():
                if proc.name() == proc_name:
                    LOGGER.debug('Killing Artella App process: {}'.format(proc.name()))
                    proc.kill()
            return True
        except RuntimeError as exc:
            msg = 'Error while close Artella app instances using psutil library | {}'.format(exc)
            self._show_error(msg)
            return False

    def _get_artella_app(self):
        """
        Returns path where Artella path is installed
        :return: str
        """

        if is_windows():
            if self._project_type == 'indie':
                artella_folder = os.path.dirname(self._get_artella_data_folder())
            else:
                artella_folder = self._get_artella_data_folder()
            return os.path.join(artella_folder, self._artella_app)
        elif is_mac():
            if self._project_type == 'indie':
                artella_folder = os.path.dirname(self._get_artella_data_folder())
                return os.path.join(artella_folder, self._artella_app)
            else:
                artella_folder = '/System/Applications'
                return os.path.join(artella_folder, 'Artella Drive.app')

    def _get_artella_program_folder(self):
        """
        Returns folder where Artella shortcuts are located
        :return: str
        """

        # TODO: This only works on Windows, find a cross-platform way of doing this

        return os.path.join(os.environ['PROGRAMDATA'], 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Artella')

    def _get_artella_launch_shortcut(self):
        """
        Returns path where Launch Artella shortcut is located
        :return: str
        """

        # TODO: This only works on Windows, find a cross-platform way of doing this

        return os.path.join(self._get_artella_program_folder(), 'Launch Artella.lnk')

    def _launch_artella_app(self):
        """
        Executes Artella App
        """

        if is_mac():
            if self._project_type == 'indie':
                artella_app_file = self._get_artella_app() + '.bundle'
            else:
                artella_app_file = self._get_artella_app()
        else:
            if self._project_type == 'indie':
                artella_app_file = self._get_artella_launch_shortcut()
            else:
                artella_app_file = self._get_artella_app() + '.exe'

        artella_app_file = artella_app_file
        LOGGER.info('Artella App File: {0}'.format(artella_app_file))

        if os.path.isfile(artella_app_file):
            LOGGER.info('Launching Artella App ...')
            LOGGER.debug('Artella App File: {0}'.format(artella_app_file))

            os.startfile('"{}"'.format(artella_app_file.replace('\\', '//')))

    def _on_open_tag_info(self):
        """
        Internal callback function that is called when tag info button is clicked by user
        Opens webpage of the release in the user browser
        """

        if self._dev:
            webbrowser.open('https://github.com/{}/releases'.format(self._repository))
        else:
            webbrowser.open('https://github.com/{}/releases/tag/{}'.format(self._repository, self._deploy_tag))

    def _on_refresh_tag(self):
        """
        Internal callback function that is called when tag refresh button is clicked by user
        Forces requirements to be in current deployment version
        """

        self._load(clean=False)

    def _on_selected_tag(self, new_index):
        """
        Internal callback function that is called when a new tag is selectged in tags combo box
        :param new_index: int
        """

        new_tag = self._deploy_tag_combo.itemText(new_index)
        if not new_tag:
            msg = 'New Tag "{}" is not valid!'.format(new_tag)
            self._show_error(msg)
            return

        res = QMessageBox.question(
            self._splash, 'Installing tag version: "{}"'.format(new_tag),
            'Are you sure you want to install this version: "{}"?'.format(new_tag),
            QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No)
        if res == QMessageBox.Yes:
            LOGGER.info("Installing tag version: {}".format(new_tag))
            self._deploy_tag = new_tag
            self._selected_tag_index = new_index
            self._set_config('tag', new_tag)
            self._load(clean=True)
        else:
            try:
                self._deploy_tag_combo.blockSignals(True)
                self._deploy_tag_combo.setCurrentIndex(self._selected_tag_index)
            finally:
                self._deploy_tag_combo.blockSignals(False)

    def _on_open_installation_folder(self):
        """
        Internal callback function that is called when the user press Open Installation Folder button
        """

        install_path = self._get_installation_path()
        if install_path and os.path.isdir(install_path) and len(os.listdir(install_path)) != 0:
            self._open_folder(install_path)
        else:
            LOGGER.warning('{} environment not installed!'.format(self._project_name))

    def _on_reinstall(self):
        """
        Internal callback function that is called when reinstall button is clicked by user
        Removes the current virtual environment setup and creates a new one
        """

        question_flags = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        res = QMessageBox.question(
            self._splash, 'Reinstalling {} Tools'.format(self.get_clean_name()),
            'Are you sure you want to reinstall {} Tools?'.format(self._project_name), question_flags)
        if res == QMessageBox.Yes:
            self._load(clean=True)

    def _on_uninstall(self, force=False):
        """
        Internal callback function that is called when the user press Uninstall button
        Removes environment variable and Tools folder
        :return:
        """

        question_flags = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No

        install_path = self._get_installation_path()
        if install_path and os.path.isdir(install_path):
            dirs_to_remove = [os.path.join(install_path, self.get_clean_name())]
            res = None
            if not force:
                res = QMessageBox.question(
                    self._splash, 'Uninstalling {} Tools'.format(self._project_name),
                    'Are you sure you want to uninstall {} Tools?\n\nFolder/s that will be removed \n\t{}'.format(
                        self._project_name, '\n\t'.join(dirs_to_remove)), question_flags)
            if res == QMessageBox.Yes or force:
                try:
                    for d in dirs_to_remove:
                        if os.path.isdir(d):
                            shutil.rmtree(d, ignore_errors=True)
                        elif os.path.isfile(d):
                            os.remove(d)
                    after_files = os.listdir(self._install_path)
                    if not after_files:
                        try:
                            os.remove(self._install_path)
                        except Exception:
                            pass
                    self._set_config(self._install_env_var, '')
                    if not force:
                        QMessageBox.information(
                            self._splash, '{} Tools uninstalled'.format(self._project_name),
                            '{} Tools uninstalled successfully! App will be closed now!'.format(self._project_name))
                    QApplication.instance().quit()
                except Exception as e:
                    self._set_config(self._install_env_var, '')
                    QMessageBox.critical(
                        self._splash, 'Error during {} Tools uninstall process'.format(self._project_name),
                        'Error during {} Tools uninstall: {} | {}\n\n'
                        'You will need to remove following folders manually:\n\n{}'.format(
                            self._project_name, e, traceback.format_exc(), '\n\t'.join(dirs_to_remove)))
        else:
            msg = '{} tools are not installed! Launch any DCC first!'.format(self._project_name)
            QMessageBox.information(
                self._splash, '{} Tools are not installed'.format(self._project_name),
                msg
            )
            LOGGER.warning(msg)

    def _run_subprocess(self, command=None, commands_list=None, close_fds=False, hide_console=True,
                        stdout=None, stderr=None, shell=True):

        if not commands_list:
            commands_list = list()

        creation_flags = 0
        if hide_console and not self._dev:
            creation_flags = 0x08000000         # No window

        stdout = stdout or subprocess.PIPE
        stderr = stderr or subprocess.PIPE

        if sys.version_info[0] == 2:
            stdin = open(os.devnull, 'wb')
        else:
            stdin = subprocess.DEVNULL

        if close_fds:
            stdout = None

        if command:
            if is_windows():
                if close_fds:
                    process = subprocess.Popen(
                        command, close_fds=close_fds, creationflags=creation_flags, stdout=stdout)
                else:
                    process = subprocess.Popen(
                        command, close_fds=close_fds, creationflags=creation_flags,
                        stdout=stdout, stdin=stdin, stderr=stderr
                    )
            elif is_mac():
                process = subprocess.Popen(command, close_fds=close_fds, stdout=stdout, shell=shell)
            else:
                process = subprocess.Popen(command, close_fds=close_fds, stdout=stdout)
        elif commands_list:
            if is_windows():
                process = subprocess.Popen(
                    commands_list, close_fds=close_fds, creationflags=creation_flags,
                    stdout=stdout, stdin=stdin, stderr=stderr)
            elif is_mac():
                process = subprocess.Popen(commands_list, close_fds=close_fds, stdout=stdout, shell=shell)
            else:
                process = subprocess.Popen(commands_list, close_fds=close_fds, stdout=stdout)
        else:
            msg = "Impossible to launch subprocess: command={}, commands_list={}, close_fds={}, hide_console={}".format(
                command, commands_list, close_fds, hide_console)
            self._show_error(msg)
            return None

        return process

    def _check_call(self, commands_list, shell=True):
        if not commands_list:
            msg = "Impossible to launch subprocess: commands_list={}".format(commands_list)
            self._show_error(msg)
            return None

        process = subprocess.check_call(commands_list, shell=shell)

        return process

    def _show_error(self, msg, title='Error'):
        LOGGER.error(msg)
        QMessageBox.critical(self._splash, title, msg)


@contextlib.contextmanager
def application():
    app = QApplication.instance()

    if not app:
        app = QApplication(sys.argv)
        yield app
        app.exec_()
    else:
        yield app


class AppErrorDialog(QDialog, object):
    def __init__(self, exc_trace, parent=None):
        self._trace = exc_trace
        super(AppErrorDialog, self).__init__(parent=parent)

        self.setWindowTitle('Artella Launcher - Error')
        self.setWindowIcon(QIcon(self._get_resource('artella_ok.png')))

        self.ui()
        self.setup_signals()

    def ui(self):

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(2, 2, 2, 2)
        self.main_layout.setSpacing(2)
        self.setLayout(self.main_layout)

        self._error_text = QPlainTextEdit(str(self._trace) if self._trace else '')
        self._error_text.setReadOnly(True)
        self._error_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.main_layout.addWidget(self._error_text)

        buttons_lyt = QHBoxLayout()
        self._copy_to_clipboard_btn = QPushButton('Copy to Clipboard')
        buttons_lyt.addStretch()
        buttons_lyt.addWidget(self._copy_to_clipboard_btn)
        self.main_layout.addLayout(buttons_lyt)

    def setup_signals(self):
        self._copy_to_clipboard_btn.clicked.connect(self._on_copy_to_clipboard)

    def _get_resource(self, resource_name):
        resource_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', resource_name)
        if not os.path.isfile(resource_path):
            resource_path = os.path.join(os.path.dirname(sys.executable), 'resources', resource_name)
            if not os.path.isfile(resource_path):
                if hasattr(sys, '_MEIPASS'):
                    resource_path = os.path.join(sys._MEIPASS, 'resources', resource_name)

        LOGGER.info("Retrieving resource: {} >>> {}".format(resource_name, resource_path))

        return resource_path

    def _on_copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self._error_text.toPlainText(), QClipboard.Clipboard)
        if clipboard.supportsSelection():
            clipboard.setText(self._error_text.toPlainText(), QClipboard.Selection)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--project-name', required=False)
    parser.add_argument('--project-type', required=False)
    parser.add_argument('--version', required=False, default="0.0.0")
    parser.add_argument('--repository', required=False)
    parser.add_argument('--icon-path', required=False, default=None)
    parser.add_argument('--splash-path', required=False, default=None)
    parser.add_argument('--script-path', required=False, default=None)
    parser.add_argument('--requirements-path', required=False, default=None)
    parser.add_argument('--artellapipe-configs-path', required=False, default=None)
    parser.add_argument('--dev', required=False, default=False, action='store_true')
    args = parser.parse_args()

    with application() as app:

        icon_path = args.icon_path
        if icon_path and os.path.isfile(icon_path):
            app.setWindowIcon(QIcon(icon_path))
        else:
            icon_path = None

        new_app = None
        valid_app = False
        try:
            new_app = ArtellaUpdater(
                app=app,
                project_name=args.project_name,
                project_type=args.project_type,
                app_version=args.version,
                deployment_repository=args.repository,
                splash_path=args.splash_path,
                script_path=args.script_path,
                requirements_path=args.requirements_path,
                artellapipe_configs_path=args.artellapipe_configs_path,
                dev=args.dev,
                update_icon=not bool(icon_path)
            )
            valid_app = True
        except Exception as exc:
            raise ArtellaUpdaterException(exc)
