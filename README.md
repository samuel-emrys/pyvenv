# pyvenv

This is a conan `python_requires` package, exposing functionality to create and manage python virtual environments.

This package exposes a number of modules:

* `PythonVirtualEnv`: Construct a `PythonVirtualEnv` object. This will allow you to set up a new virtual environment, or manage an existing one.
* `create`: Create a virtual environment
* `setup_entry_points`: Create an entry point for a package installed in the virtual environment
* `entry_points`: Retrieve the entry points associated with a particular package in the virtual environment
* `which`: Retrieve the path to an executable within the virtual environment
* `args_to_string`: Convert a list of arguments to a command line string in an operating system agnostic way
* `make_relocatable`: Patch files in the virtualenv such that it can be relocated on the local file system.

## Usage

Create a new python virtual environment:

```python
class PythonVirtualEnvironment(ConanFile):
    python_requires = "pyvenv/[>=0.2.2]@mtolympus/stable"

    def package(self):
        requirements = ["sphinx==4.4.0", "sphinx_rtd_theme=0.5.3", "matplotlib==3.5.0"]
        args_to_string = self.python_requires["pyvenv"].module.args_to_string
        venv = self.python_requires["pyvenv"].module.PythonVirtualEnv(self)

        # You can use any of the following methods to install packages into the virtualenv
        venv.create(folder=os.path.join(self.package_folder), requirements=requirements)
        self.run(args_to_string([venv.python, "-mpip", "install", "sphinx-multiversion"], scope="conanbuild")
        with venv.activate():
            # NOTE: Do not use `venv.pip` outside of an `activate` block - it will embed the incorrect interpreter
            # into the script
            self.run(args_to_string([venv.pip, "install", "black"], scope="conanbuild")

        venv.setup_entry_points("sphinx", os.path.join(self.package_folder, "bin"))
        venv.make_relocatable(env_folder=self.package_folder) # Only needs to be run when packages are installed outside of `venv.create()`
```

Manage an existing python virtual environment:

```python
from pathlib import Path

class PythonVirtualEnvironment(ConanFile):
    python_requires = "pyvenv/[>=0.2.2]@mtolympus/stable"

    @property
    def binpath(self):
        return "Scripts" if sys.platform == "win32" else "bin"

    def package(self):
        python_envdir = Path(Path.home(), "venv")
        path = Path(python_envdir, self.binpath, "python")
        realname = path.resolve(strict=True).name
        interpreter = str(path.with_name(realname))
        venv = self.python_requires["pyvenv"].module.PythonVirtualEnv(
            self._conanfile,
            interpreter=interpreter,
            env_folder=python_envdir,
        )

        entry_points = venv.entry_points("sphinx")
        # Get the names of the sphinx executables, i.e. sphinx-build, sphinx-quickstart, sphinx-apidoc, sphinx-autogen
        console_scripts = entry_points.get("console_scripts", []) 
```

An example of a package that uses this is the [`python-virtualenv/system`](https://github.com/samuel-emrys/python-virtualenv) package.

See also the [CMakePythonDeps](https://github.com/samuel-emrys/cmake-python-deps) generator, which is designed to be used in conjunction with PythonVirtualEnv.

This has been developed with significant inspiration from, and uses `pyvenv` code largely developed by [thorntonryan/conan-pyvenv](https://github.com/thorntonryan/conan-pyvenv), and modified by Samuel Dowling.
