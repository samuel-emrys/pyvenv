# pyvenv

This is a conan `python_requires` package, exposing two main pieces of functionality:

1. A `venv` class, which can be used to create and manage python virtual environments.
2. A `CMakePythonEnvironment` generator, which can be used to generate CMake targets for executables in a python virtual environment.

Appropriate usage of each of these will be detailed below.

## `venv` Class

This package exposes a number of methods:

* `venv`: Construct a `venv` object. This will allow you to set up a new virtual environment, or manage an existing one.
* `create`: Create a virtual environment
* `setup_entry_points`: Create an entry point for a package installed in the virtual environment
* `entry_points`: Retrieve the entry points associated with a particular package in the virtual environment
* `which`: Retrieve the path to an executable within the virtual environment

### Usage

Create a new python virtual environment:

```python
class PythonVirtualEnvironment(ConanFile):
    python_requires = "pyvenv/0.1.0"

    def package(self):
        venv = self.python_requires["pyvenv"].module.venv(self)
        venv.create(folder=os.path.join(self.package_folder))
        self.run(
            tools.args_to_string([
                venv.pip, "install", "sphinx==4.4.0", "sphinx_rtd_theme=0.5.3", "matplotlib==3.5.0",
            ])
        )
        venv.setup_entry_points("sphinx", os.path.join(self.package_folder, "bin"))
```

Manage an existing python virtual environment:

```python
from pathlib import Path

class PythonVirtualEnvironment(ConanFile):
    python_requires = "pyvenv/0.1.0"

    @property
    def binpath(self):
        return "Scripts" if sys.platform == "win32" else "bin"

    def package(self):
        python_envdir = Path(Path.home(), "venv")
        path = Path(python_envdir, self.binpath, "python")
        realname = path.resolve(strict=True).name
        interpreter = str(path.with_name(realname))
        venv = self.python_requires["pyvenv"].module.venv(
            self._conanfile,
            python=interpreter,
            env_folder=python_envdir,
        )

        entry_points = venv.entry_points("sphinx")
        # Get the names of the sphinx executables, i.e. sphinx-build, sphinx-quickstart, sphinx-apidoc, sphinx-autogen
        console_scripts = entry_points.get("console_scripts", []) 
```

An example of a package that uses this is the [`python-virtualenv/system`](https://github.com/samuel-emrys/python-virtualenv) package.

## `CMakePythonEnvironment` Generator

For a recipe to be consumed using the `CMakePythonEnvironment` generator, populate the `self.user_info.python_requirements` and `self.user_info.python_env` `package_info()` variables as below:

```python
class PythonVirtualEnvironment(ConanFile):

    def package_info(self):
        requirements = [
            "sphinx==4.4.0",
            "sphinx_rtd_theme",
            "sphinx_book_theme==0.3.2",
            "pygments",
        ]
        self.user_info.python_requirements = json.dumps(requirements)
        self.user_info.python_envdir = self.package_folder
```

* `user_info.python_requirements`: This is expected to be a JSON string containing a list of the packages and their versions to be installed into the virtual environment.
* `user_info.python_envdir`: A string representing the path to the python virtual environment.

An example of a package that uses this is the [`python-virtualenv/system`](https://github.com/samuel-emrys/python-virtualenv) package.

To consume a recipe that has populated the above variables, simply specify `CMakePythonEnvironment` as the generator to use in the consumer `conanfile.py`:

```python
class ExamplePythonConan(ConanFile):
    # ...
    python_requires = "pyvenv/0.1.0"

    def generate(self):
        py = python_requires["pyvenv"].modules.CMakePythonEnvironment(self)
        py.generate()
```

This generator will create CMake targets named for the package, and it's executables to allow you to use them in your CMake recipes. To illustrate, if you were to install `sphinx` in a virtual environment, the entry points `sphinx-build`, `sphinx-quickstart`, `sphinx-apidoc` and `sphinx-autogen` would be created in the virtual environment. The corresponding CMake targets would be:

```cmake
sphinx::sphinx-build
sphinx::sphinx-quickstart
sphinx::sphinx-apidoc
sphinx::sphinx-autogen
```

This means that a minimal `docs/CMakeLists.txt` for a sphinx dependency might look like the following:

```cmake
find_package(sphinx REQUIRED)

# Sphinx configuration
set(SPHINX_SOURCE ${CMAKE_CURRENT_SOURCE_DIR})
set(SPHINX_BUILD ${CMAKE_CURRENT_BINARY_DIR}/sphinx)
set(SPHINX_INDEX_FILE ${SPHINX_BUILD}/index.html)

# Only regenerate Sphinx when:
# - Doxygen has rerun
# - Our doc files have been updated
# - The Sphinx config has been updated
add_custom_command(
  OUTPUT ${SPHINX_INDEX_FILE}
  DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/index.rst
  COMMAND sphinx::sphinx-build -b html ${SPHINX_SOURCE} ${SPHINX_BUILD}
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
  COMMENT "Generating documentation with Sphinx")

add_custom_target(sphinx ALL DEPENDS ${SPHINX_INDEX_FILE})

# Add an install target to install the docs
include(GNUInstallDirs)
install(DIRECTORY ${SPHINX_BUILD}/ DESTINATION ${CMAKE_INSTALL_DOCDIR})

```

An example of this being used in conjunction with `python-virtualenv` is [`sphinx-consumer`](https://github.com/samuel-emrys/sphinx-consumer).

This has been developed with significant inspiration from, and uses `pyvenv` code largely developed by [thorntonryan/conan-pyvenv](https://github.com/thorntonryan/conan-pyvenv), and modified by Samuel Dowling.
