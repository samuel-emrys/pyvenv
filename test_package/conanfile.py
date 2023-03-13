import os

from conan import ConanFile
from conan.tools.layout import basic_layout
from conan.tools.build import cross_building


class PyvenvTestConan(ConanFile):
    settings = "os", "build_type", "arch", "compiler"
    apply_env = False
    test_type = "explicit"
    build_policy = "missing"
    _venv = None

    def config_options(self):
        del self.settings.build_type
        del self.settings.arch
        del self.settings.compiler

    def _configure_venv(self):
        venv = self.python_requires["pyvenv"].module.PythonVirtualEnv
        if not self._venv:
            self._venv = venv(self)
        return self._venv

    def build(self):
        args_to_string = self.python_requires["pyvenv"].module.args_to_string
        venv = self._configure_venv()
        # Any of the three following techniques to install are supported
        venv.create(folder=os.path.join(self.build_folder), requirements=["sphinx==4.4.0"])
        self.run(args_to_string([venv.python, "-mpip", "install", "sphinx-rtd-theme"]), env="conanbuild")
        with venv.activate():
            # Invoking venv.pip _must_ be in an activated virtualenv
            # If you don't do this, the system interpreter will be used and the package won't be patchable
            self.run(args_to_string([venv.pip, "install", "sphinx-multiversion"]), env="conanbuild")
        # make_relocatable is only necessary for packages installed outside of `venv.create`
        venv.make_relocatable(env_folder=self.build_folder)

    def layout(self):
        basic_layout(self)

    def test(self):
        bindir = "Scripts" if self.settings.os == "Windows" else "bin"
        if not cross_building(self):
            args_to_string = self.python_requires["pyvenv"].module.args_to_string
            cmd = [os.path.join(self.build_folder, bindir, "sphinx-build"), "--help"]
            self.run(args_to_string(cmd), env="conanrun")
