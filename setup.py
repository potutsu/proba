from setuptools import setup, find_packages

setup(
    name="proba",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests>=2.28.0",
        "python-dotenv>=1.0.0",
        "rich>=13.0.0",
    ],
    extras_require={
        "windows": ["windows-curses>=2.3"],
    },
    entry_points={
        "console_scripts": [
            "proba=proba.cli:main",
            "antii=proba.antii.cli:main",
        ],
    },
    python_requires=">=3.10",
)
