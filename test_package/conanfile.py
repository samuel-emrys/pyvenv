import os
import shutil
import pathlib

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

    @property
    def _bindir(self):
        return "Scripts" if self.settings.os == "Windows" else "bin"

    def build(self):
        args_to_string = self.python_requires["pyvenv"].module.args_to_string
        venv = self._configure_venv()
        # Any of the three following techniques to install are supported
        requirements=["sphinx==4.4.0"]
        venv.create(folder=os.path.join(self.build_folder), requirements=requirements)
        #self.run(args_to_string([venv.python, "-mpip", "install", "sphinx-rtd-theme"]), env="conanbuild")
        #with venv.activate():
        #    # Invoking venv.pip _must_ be in an activated virtualenv
        #    # If you don't do this, the system interpreter will be used and the package won't be patchable
        #    self.run(args_to_string([venv.pip, "install", "sphinx-multiversion"]), env="conanbuild")
        # make_relocatable is only necessary for packages installed outside of `venv.create`
        for requirement in requirements:
            package = requirement.split("==")[0]
            venv.setup_entry_points(str(package), os.path.join(self.build_folder, self._bindir))

        venv.make_relocatable(env_folder=self.build_folder)


    def layout(self):
        basic_layout(self)

    def _rm_directory_contents(self, directory):
        directory = pathlib.Path(directory)
        for item in directory.glob("*/*/*"):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                os.remove(item)

    def test(self):
        if not cross_building(self):
            args_to_string = self.python_requires["pyvenv"].module.args_to_string
            cmd = [os.path.join(self.build_folder, self._bindir, "sphinx-build"), "--help"]
            self.run(args_to_string(cmd), env="conanrun")

            self.output.info("Testing ability to relocate virtualenv")
            new_build_folder = f"{self.build_folder}-relocated"
            # Can't move self.build_folder because this process has it open, so
            # copy instead and remove all files inside
            shutil.copytree(self.build_folder, new_build_folder,
                             dirs_exist_ok=True)
            #shutil.rmtree(os.path.join(self.build_folder, self._bindir))
            self._rm_directory_contents(self.build_folder)
            cmd = [os.path.join(new_build_folder, self._bindir, "sphinx-build"), "--help"]
            self.run(args_to_string(cmd), env="conanrun")
