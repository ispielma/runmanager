#####################################################################
#                                                                   #
# /globals_file.py                                                  #
#                                                                   #
# Copyright 2026, Ian Spielman                                      #
#                                                                   #
# This file is part of the program runmanager, in the labscript     #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################

"""Storage helpers for runmanager globals files.

This module keeps the globals-file schema and CRUD operations in one place.
Canonical editable globals files are TOML-backed. Legacy HDF5 globals files
remain readable as migration inputs and can be converted to TOML when the user
wants to edit them.
"""

import copy
import os

import labscript_utils.h5_lock
import h5py

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

import tomli_w


CANONICAL_FORMAT = "runmanager_globals"
CURRENT_VERSION = 1
LEGACY_HDF5_EXTENSIONS = {".h5", ".hdf5"}


class GlobalsFileError(Exception):
    pass


class LegacyGlobalsFileError(GlobalsFileError):
    pass


def _ensure_str(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def _blank_record():
    return {
        "default": "",
        "units": "",
        "scan_enabled": False,
        "scan": "",
        "expansion": "",
    }


def _normalise_record(record):
    normalised = _blank_record()
    normalised.update(
        {
            "default": _ensure_str(record.get("default", "")),
            "units": _ensure_str(record.get("units", "")),
            "scan_enabled": bool(record.get("scan_enabled", False)),
            "scan": _ensure_str(record.get("scan", "")),
            "expansion": _ensure_str(record.get("expansion", "")),
        }
    )
    if not normalised["scan_enabled"]:
        normalised["expansion"] = ""
    elif not normalised["expansion"]:
        normalised["expansion"] = "outer"
    return normalised


def _new_document():
    return {
        "format": CANONICAL_FORMAT,
        "version": CURRENT_VERSION,
        "groups": {},
    }


def _normalise_document(data):
    document = _new_document()
    document["format"] = _ensure_str(data.get("format", CANONICAL_FORMAT))
    document["version"] = int(data.get("version", CURRENT_VERSION))
    groups = data.get("groups", {})
    for group_name, group_data in groups.items():
        globals_data = group_data.get("globals", {}) if isinstance(group_data, dict) else {}
        document["groups"][_ensure_str(group_name)] = {"globals": {}}
        for global_name, record in globals_data.items():
            document["groups"][_ensure_str(group_name)]["globals"][_ensure_str(global_name)] = (
                _normalise_record(record)
            )
    return document


def detect_backend(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".toml":
        return "toml"
    if ext in LEGACY_HDF5_EXTENSIONS:
        return "legacy_hdf5"
    raise GlobalsFileError(
        "Unsupported globals file format for %s. Expected .toml, .h5, or .hdf5."
        % filename
    )


def is_legacy_hdf5(filename):
    return detect_backend(filename) == "legacy_hdf5"


def default_toml_path(filename):
    root, _ = os.path.splitext(filename)
    return root + ".toml"


def load_document(filename):
    backend = detect_backend(filename)
    if backend == "toml":
        return load_toml_document(filename)
    return load_legacy_hdf5_document(filename)


def load_toml_document(filename):
    with open(filename, "rb") as f:
        data = tomllib.load(f)
    document = _normalise_document(data)
    if document["format"] != CANONICAL_FORMAT:
        raise GlobalsFileError(
            "Unsupported globals TOML format %r in %s." % (document["format"], filename)
        )
    return document


def load_legacy_hdf5_document(filename):
    document = _new_document()
    with h5py.File(filename, "r") as f:
        try:
            globals_root = f["globals"]
        except KeyError as exc:
            raise GlobalsFileError("%s does not contain a /globals group." % filename) from exc
        for group_name in globals_root:
            group = f["globals"][group_name]
            units_group = group.get("units")
            values = dict(group.attrs)
            units = dict(units_group.attrs) if units_group is not None else {}
            document["groups"][group_name] = {"globals": {}}
            for global_name, value in values.items():
                document["groups"][group_name]["globals"][global_name] = {
                    "default": _ensure_str(value),
                    "units": _ensure_str(units.get(global_name, "")),
                    "scan_enabled": False,
                    "scan": "",
                    "expansion": "",
                }
    return document


def save_document(filename, document):
    if detect_backend(filename) != "toml":
        raise LegacyGlobalsFileError(
            "Legacy HDF5 globals files are import-only. Save to a .toml globals file instead."
        )
    document = _normalise_document(document)
    directory = os.path.dirname(filename)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filename, "wb") as f:
        tomli_w.dump(document, f)


def new_globals_file(filename):
    save_document(filename, _new_document())


def get_group_names(filename):
    document = load_document(filename)
    return list(document["groups"])


def get_globals_details(groups):
    details = {}
    for filepath in set(groups.values()):
        document = load_document(filepath)
        groups_from_file = [group_name for group_name, path in groups.items() if path == filepath]
        for group_name in groups_from_file:
            group = document["groups"][group_name]["globals"]
            details[group_name] = {
                global_name: copy.deepcopy(record) for global_name, record in group.items()
            }
    return details


def get_globals(groups):
    details = get_globals_details(groups)
    sequence_globals = {}
    for group_name, globals_data in details.items():
        sequence_globals[group_name] = {}
        for global_name, record in globals_data.items():
            expression = record["scan"] if record["scan_enabled"] else record["default"]
            expansion = record["expansion"] if record["scan_enabled"] else ""
            sequence_globals[group_name][global_name] = (
                expression,
                record["units"],
                expansion,
            )
    return sequence_globals


def _require_editable(filename):
    if is_legacy_hdf5(filename):
        raise LegacyGlobalsFileError(
            "Legacy HDF5 globals files are import-only. Convert this file to .toml before editing."
        )


def _get_group(document, groupname):
    try:
        return document["groups"][groupname]["globals"]
    except KeyError as exc:
        raise GlobalsFileError('No such group "%s".' % groupname) from exc


def new_group(filename, groupname):
    _require_editable(filename)
    document = load_document(filename)
    if groupname in document["groups"]:
        raise GlobalsFileError("Can't create group: target name already exists.")
    document["groups"][groupname] = {"globals": {}}
    save_document(filename, document)


def copy_group(source_filename, source_groupname, dest_filename, delete_source_group=False):
    source_document = load_document(source_filename)
    if dest_filename is None:
        dest_filename = source_filename
    _require_editable(dest_filename)
    dest_document = load_document(dest_filename)
    try:
        source_group = source_document["groups"][source_groupname]["globals"]
    except KeyError as exc:
        raise GlobalsFileError(
            'Can\'t copy: there is no group "%s".' % source_groupname
        ) from exc
    i = 0 if not delete_source_group else 1
    dest_groupname = source_groupname
    while dest_groupname in dest_document["groups"]:
        if i == 0:
            dest_groupname = "%s_copy" % source_groupname
        else:
            dest_groupname = "%s(%d)" % (source_groupname, i)
        i += 1
    dest_document["groups"][dest_groupname] = {
        "globals": copy.deepcopy(source_group),
    }
    save_document(dest_filename, dest_document)
    return dest_groupname


def rename_group(filename, oldgroupname, newgroupname):
    _require_editable(filename)
    if oldgroupname == newgroupname:
        return
    document = load_document(filename)
    if newgroupname in document["groups"]:
        raise GlobalsFileError("Can't rename group: target name already exists.")
    try:
        document["groups"][newgroupname] = document["groups"].pop(oldgroupname)
    except KeyError as exc:
        raise GlobalsFileError('No such group "%s".' % oldgroupname) from exc
    save_document(filename, document)


def delete_group(filename, groupname):
    _require_editable(filename)
    document = load_document(filename)
    try:
        del document["groups"][groupname]
    except KeyError as exc:
        raise GlobalsFileError('No such group "%s".' % groupname) from exc
    save_document(filename, document)


def new_global(filename, groupname, globalname):
    _require_editable(filename)
    document = load_document(filename)
    group = _get_group(document, groupname)
    if globalname in group:
        raise GlobalsFileError("Can't create global: target name already exists.")
    group[globalname] = _blank_record()
    save_document(filename, document)


def rename_global(filename, groupname, oldglobalname, newglobalname):
    _require_editable(filename)
    if oldglobalname == newglobalname:
        return
    document = load_document(filename)
    group = _get_group(document, groupname)
    if newglobalname in group:
        raise GlobalsFileError("Can't rename global: target name already exists.")
    try:
        group[newglobalname] = group.pop(oldglobalname)
    except KeyError as exc:
        raise GlobalsFileError('No such global "%s".' % oldglobalname) from exc
    save_document(filename, document)


def delete_global(filename, groupname, globalname):
    _require_editable(filename)
    document = load_document(filename)
    group = _get_group(document, groupname)
    try:
        del group[globalname]
    except KeyError as exc:
        raise GlobalsFileError('No such global "%s".' % globalname) from exc
    save_document(filename, document)


def get_global_record(filename, groupname, globalname):
    document = load_document(filename)
    group = _get_group(document, groupname)
    try:
        return copy.deepcopy(group[globalname])
    except KeyError as exc:
        raise GlobalsFileError('No such global "%s".' % globalname) from exc


def set_global_record(filename, groupname, globalname, record):
    _require_editable(filename)
    document = load_document(filename)
    group = _get_group(document, groupname)
    if globalname not in group:
        raise GlobalsFileError('No such global "%s".' % globalname)
    group[globalname] = _normalise_record(record)
    save_document(filename, document)


def get_field(filename, groupname, globalname, field):
    return get_global_record(filename, groupname, globalname)[field]


def set_field(filename, groupname, globalname, field, value):
    record = get_global_record(filename, groupname, globalname)
    if field == "scan_enabled":
        record[field] = bool(value)
        if not record[field]:
            record["expansion"] = ""
        elif not record["expansion"]:
            record["expansion"] = "outer"
    else:
        record[field] = value
    set_global_record(filename, groupname, globalname, record)


def convert_to_toml(source_filename, dest_filename):
    document = load_document(source_filename)
    save_document(dest_filename, document)
    return dest_filename
