# Copyright (C) 2014 New York University
# This file is part of ReproZip which is released under the Revised BSD License
# See file LICENSE for full license details.

"""Packing logic for reprozip.

This module contains the :func:`~reprozip.pack.pack` function and associated
utilities that are used to build the .rpz pack file from the trace SQLite file
and config YAML.
"""

from __future__ import unicode_literals

import itertools
import logging
import os
from rpaths import Path
import sqlite3
import sys
import tarfile

from reprozip import __version__ as reprozip_version
from reprozip.common import FILE_WRITE, FILE_WDIR, File, \
    load_config, save_config
from reprozip.tracer.linux_pkgs import identify_packages
from reprozip.tracer.trace import merge_files
from reprozip.utils import PY3


def expand_patterns(patterns):
    files = set()
    dirs = set()

    # Finds all matching paths
    for pattern in patterns:
        for path in Path('/').recursedir(pattern):
            if path.is_dir():
                dirs.add(path)
            else:
                files.add(path)

    # Don't include directories whose files are included
    non_empty_dirs = set([Path('/')])
    for p in files | dirs:
        path = Path('/')
        for c in p.components[1:]:
            path = path / c
            non_empty_dirs.add(path)

    # Builds the final list
    return [File(p) for p in itertools.chain(dirs - non_empty_dirs, files)]


def canonicalize_config(runs, packages, other_files, additional_patterns,
                        sort_packages):
    add_files = expand_patterns(additional_patterns)
    if sort_packages:
        add_files, add_packages = identify_packages(add_files)
    else:
        add_packages = []
    other_files, packages = merge_files(add_files, add_packages,
                                        other_files, packages)
    return runs, packages, other_files


def list_directories(database):
    if PY3:
        # On Python 3, connect() only accepts unicode
        conn = sqlite3.connect(str(database))
    else:
        conn = sqlite3.connect(database.path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    executed_files = cur.execute(
            '''
            SELECT name, mode
            FROM opened_files
            WHERE mode = ? OR mode = ?
            ''',
            (FILE_WDIR, FILE_WRITE))
    executed_files = ((Path(n), m) for n, m in executed_files)
    # If WDIR, the name is a folder that was used as working directory
    # If WRITE, the name is a file that was written to; its directory must
    # exist
    result = set(n if m == FILE_WDIR else n.parent
                 for n, m in executed_files)
    cur.close()
    conn.close()
    return result


def data_path(filename, prefix=Path('DATA')):
    """Computes the filename to store in the archive.

    Turns an absolute path containing '..' into a filename without '..', and
    prefixes with DATA/.

    Example:

    >>> data_path(PosixPath('/var/lib/../../../../tmp/test'))
    PosixPath(b'DATA/tmp/test')
    >>> data_path(PosixPath('/var/lib/../www/index.html'))
    PosixPath(b'DATA/var/www/index.html')
    """
    return prefix / filename.split_root()[1]


class PackBuilder(object):
    def __init__(self, filename):
        self.tar = tarfile.open(str(filename), 'w:gz')
        self.seen = set()

    def add(self, name, arcname, *args, **kwargs):
        from rpaths import PosixPath
        assert isinstance(name, PosixPath)
        assert isinstance(arcname, PosixPath)
        self.tar.add(str(name), str(arcname), *args, **kwargs)

    def add_data(self, filename):
        if filename in self.seen:
            return
        path = Path('/')
        for c in filename.components[1:]:
            path = path / c
            if path in self.seen:
                continue
            logging.debug("%s -> %s" % (path, data_path(path)))
            self.tar.add(str(path), str(data_path(path)), recursive=False)
            self.seen.add(path)

    def close(self):
        self.tar.close()
        self.seen = None


def pack(target, directory, sort_packages):
    """Main function for the pack subcommand.
    """
    if target.exists():
        # Don't overwrite packs...
        sys.stderr.write("Error: Target file exists!\n")
        sys.exit(1)

    # Reads configuration
    configfile = directory / 'config.yml'
    if not configfile.is_file():
        sys.stderr.write("Error: Configuration file does not exist!\n"
                         "Did you forget to run 'reprozip trace'?\n"
                         "If not, you might want to use --dir to specify an "
                         "alternate location.\n")
        sys.exit(1)
    runs, packages, other_files, additional_patterns = load_config(
            configfile,
            canonical=False)

    # Canonicalize config (re-sort, expand 'additional_files' patterns)
    runs, packages, other_files = canonicalize_config(
            runs, packages, other_files, additional_patterns, sort_packages)

    logging.info("Creating pack %s..." % target)
    tar = PackBuilder(target)

    # Stores the original trace
    trace = directory / 'trace.sqlite3'
    if trace.is_file():
        tar.add(trace, Path('METADATA/trace.sqlite3'))

    # Add the files from the packages
    for pkg in packages:
        if pkg.packfiles:
            logging.info("Adding files from package %s..." % pkg.name)
            files = []
            for f in pkg.files:
                if not Path(f.path).exists():
                    logging.warning("Missing file %s from package %s" % (
                                    f.path, pkg.name))
                else:
                    tar.add_data(f.path)
                    files.append(f)
            pkg.files = files
        else:
            logging.info("NOT adding files from package %s" % pkg.name)

    # Add the rest of the files
    logging.info("Adding other files...")
    files = []
    for f in other_files:
        if not Path(f.path).exists():
            logging.warning("Missing file %s" % f.path)
        else:
            tar.add_data(f.path)
            files.append(f)
    other_files = files

    # Makes sure all the directories used as working directories are packed
    # (they already do if files from them are used, but empty directories do
    # not get packed inside a tar archive)
    for directory in list_directories(trace):
        if directory.is_dir():
            tar.add_data(directory)

    logging.info("Adding metadata...")
    # Stores pack version
    fd, manifest = Path.tempfile(prefix='reprozip_', suffix='.txt')
    os.close(fd)
    try:
        with manifest.open('wb') as fp:
            fp.write(b'REPROZIP VERSION 1\n')
        tar.add(manifest, Path('METADATA/version'))
    finally:
        manifest.remove()

    # Stores canonical config
    fd, can_configfile = Path.tempfile(suffix='.yml', prefix='rpz_config_')
    os.close(fd)
    try:
        save_config(can_configfile, runs, packages, other_files,
                    reprozip_version, canonical=True)

        tar.add(can_configfile, Path('METADATA/config.yml'))
    finally:
        can_configfile.remove()

    tar.close()
