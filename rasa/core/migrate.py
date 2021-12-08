import copy
import shutil
from pathlib import Path
from typing import List, Dict, Text, Any, Tuple, Optional, Union

import rasa.shared.utils.io
import rasa.shared.utils.cli
from rasa.shared.constants import (
    REQUIRED_SLOTS_KEY,
    IGNORED_INTENTS,
    DEFAULT_DOMAIN_PATH,
)
from rasa.shared.core.constants import ACTIVE_LOOP, REQUESTED_SLOT
from rasa.shared.core.domain import (
    KEY_ENTITIES,
    KEY_SLOTS,
    KEY_FORMS,
    Domain,
)
from rasa.shared.core.slot_mappings import SlotMapping
from rasa.shared.exceptions import RasaException


def _create_back_up(
    domain_file: Path, backup_location: Path
) -> Union[List[Any], Dict[Text, Any]]:
    """Makes a backup and returns the content of the file."""
    original_content = rasa.shared.utils.io.read_yaml_file(domain_file)
    rasa.shared.utils.io.write_yaml(
        original_content, backup_location, should_preserve_key_order=True
    )
    return original_content


def _get_updated_mapping_condition(
    condition: Dict[Text, Text], mapping: Dict[Text, Any], slot_name: Text
) -> Dict[Text, Text]:
    if mapping.get("type") not in [
        str(SlotMapping.FROM_ENTITY),
        str(SlotMapping.FROM_TRIGGER_INTENT),
    ]:
        return {**condition, REQUESTED_SLOT: slot_name}
    return condition


def _get_updated_or_new_mappings(
    existing_mappings: List[Dict[Text, Any]],
    new_mappings: List[Dict[Text, Any]],
    condition: Dict[Text, Text],
    slot_name: Text,
) -> List[Dict[Text, Any]]:
    updated_mappings = []

    for existing_mapping in existing_mappings:
        mapping_copy = copy.deepcopy(existing_mapping)

        conditions = existing_mapping.pop("conditions", [])
        if existing_mapping in new_mappings:
            new_mappings.remove(existing_mapping)
            conditions.append(
                _get_updated_mapping_condition(condition, existing_mapping, slot_name)
            )
            existing_mapping.update({"conditions": conditions})
            updated_mappings.append(existing_mapping)
        else:
            updated_mappings.append(mapping_copy)

    for mapping in new_mappings:
        mapping.update(
            {
                "conditions": [
                    _get_updated_mapping_condition(condition, mapping, slot_name)
                ]
            }
        )
        updated_mappings.append(mapping)

    return updated_mappings


def _migrate_form_slots(
    domain: Dict[Text, Any]
) -> Tuple[Dict[Any, Dict[str, Any]], Optional[Any]]:
    updated_slots = domain.get(KEY_SLOTS, {})
    forms = domain.get(KEY_FORMS, {})

    new_forms = {}

    for form_name, form_data in forms.items():
        ignored_intents = form_data.pop(IGNORED_INTENTS, [])
        if REQUIRED_SLOTS_KEY in form_data:
            form_data = form_data.get(REQUIRED_SLOTS_KEY, {})

        required_slots = []

        for slot_name, mappings in form_data.items():
            condition = {ACTIVE_LOOP: form_name}
            slot_properties = updated_slots.get(slot_name, {})
            existing_mappings = slot_properties.get("mappings", [])
            updated_mappings = _get_updated_or_new_mappings(
                existing_mappings, mappings, condition, slot_name
            )
            slot_properties.update({"mappings": updated_mappings})
            updated_slots[slot_name] = slot_properties

            required_slots.append(slot_name)

        new_forms[form_name] = {
            IGNORED_INTENTS: ignored_intents,
            REQUIRED_SLOTS_KEY: required_slots,
        }
    return new_forms, updated_slots


def _migrate_auto_fill(
    slot_name: Text, properties: Dict[Text, Any], entities: List[Text],
) -> Dict[Text, Any]:
    if slot_name in entities and properties.get("auto_fill", True) is True:
        from_entity_mapping = {
            "type": str(SlotMapping.FROM_ENTITY),
            "entity": slot_name,
        }
        mappings = properties.get("mappings", [])
        if from_entity_mapping not in mappings:
            mappings.append(from_entity_mapping)
            properties.update({"mappings": mappings})

    if "auto_fill" in properties:
        del properties["auto_fill"]

    return properties


def _migrate_custom_slots(
    slot_name: Text, properties: Dict[Text, Any]
) -> Dict[Text, Any]:
    if not properties.get("mappings"):
        properties.update({"mappings": [{"type": "custom"}]})

        rasa.shared.utils.io.raise_warning(
            f"A custom mapping was added to slot '{slot_name}'. "
            f"Please double-check this is correct.",
            UserWarning,
        )

    return properties


def _migrate_auto_fill_and_custom_slots(
    domain: Dict[Text, Any], slots: Dict[Text, Any]
) -> Dict[Text, Any]:
    new_slots = {}
    entities = domain.get(KEY_ENTITIES, [])

    for slot_name, properties in slots.items():
        updated_properties = _migrate_auto_fill(slot_name, properties, entities)
        updated_properties = _migrate_custom_slots(slot_name, updated_properties)

        new_slots[slot_name] = updated_properties
    return new_slots


def _assemble_new_domain(
    domain_file: Path, new_forms: Dict[Text, Any], new_slots: Dict[Text, Any]
) -> Dict[Text, Any]:
    original_content = rasa.shared.utils.io.read_yaml_file(domain_file)
    new_domain: Dict[Text, Any] = {}
    for key, value in original_content.items():
        if key == KEY_SLOTS:
            new_domain.update({key: new_slots})
        elif key == KEY_FORMS:
            new_domain.update({key: new_forms})
        elif key == "version":
            new_domain.update({key: '"3.0"'})
        else:
            new_domain.update({key: value})
    return new_domain


def _write_final_domain(
    domain_file: Path, new_forms: Dict, new_slots: Dict, out_file: Path,
) -> None:
    if domain_file.is_dir():
        for file in domain_file.iterdir():
            if not Domain.is_domain_file(file):
                continue
            new_domain = _assemble_new_domain(file, new_forms, new_slots)
            rasa.shared.utils.io.write_yaml(new_domain, out_file / file.name, True)
    else:
        new_domain = _assemble_new_domain(domain_file, new_forms, new_slots)
        rasa.shared.utils.io.write_yaml(new_domain, out_file, True)


def _migrate_domain_files(
    domain_path: Path, backup_location: Path, out_path: Path
) -> Dict[Text, Any]:
    """Migrates files that only need a version update and collects the remaining info.

    Moreover, backups will be created from all domain files that can be found in the
    given domain directory.

    Args:
        domain_path: directory containing domain files
        backup_location: where to backup all domain files
        out_path: location where to store the migrated files
    """
    slots = {}
    forms = {}
    entities = []

    domain_files = [
        file for file in domain_path.iterdir() if Domain.is_domain_file(file)
    ]

    if not domain_files:
        raise RasaException(
            f"The domain directory '{domain_path}' does not contain any domain files. "
            f"Please make sure to include these for a successful migration."
        )

    for file in domain_files:

        backup = backup_location / file.name
        original_content = _create_back_up(file, backup)

        if KEY_SLOTS not in original_content and KEY_FORMS not in original_content:
            if isinstance(original_content, dict):
                original_content.update({"version": '"3.0"'})

            # this is done so that the other domain files can be moved
            # in the migrated directory
            rasa.shared.utils.io.write_yaml(
                original_content, out_path / file.name, True
            )
        elif KEY_SLOTS in original_content and slots:
            raise RasaException(
                f"Domain files with multiple '{KEY_SLOTS}' "
                f"sections were provided. Please group these sections "
                f"in one file only to prevent content duplication across "
                f"multiple files. "
            )
        elif KEY_FORMS in original_content and forms:
            raise RasaException(
                f"Domain files with multiple '{KEY_FORMS}' "
                f"sections were provided. Please group these sections "
                f"in one file only to prevent content duplication across "
                f"multiple files. "
            )

        slots.update(original_content.get(KEY_SLOTS, {}))
        forms.update(original_content.get(KEY_FORMS, {}))
        entities.extend(original_content.get(KEY_ENTITIES, {}))

    if not slots or not forms:
        raise RasaException(
            f"The files you have provided in '{domain_path}' are missing slots "
            f"or forms. Please make sure to include these for a "
            f"successful migration."
        )

    return {KEY_SLOTS: slots, KEY_FORMS: forms, KEY_ENTITIES: entities}


def migrate_domain_format(
    domain_path: Union[Text, Path], out_path: Optional[Union[Text, Path]],
) -> None:
    """Converts 2.0 domain to 3.0 format."""
    domain_path = Path(domain_path)
    out_path = Path(out_path) if out_path else None

    domain_parent_dir = domain_path.parent
    migrate_file_only = domain_path.is_file()

    # Ensure the backup location does not exist yet
    # Note: We demand that file as well as folder with this name gets deleted before
    # the command is run to avoid confusion afterwards.
    suffix = "original_domain"
    suffix = f"{suffix}.yml" if migrate_file_only else suffix
    backup_location = domain_parent_dir / suffix
    if backup_location.exists():
        backup_location_str = "directory" if backup_location.isdir() else "file"
        raise RasaException(
            f"The domain from '{domain_path}' could not be migrated since the "
            f"a {backup_location_str} {backup_location} already exists."
            f"Please remove that there is no file or folder at {backup_location}."
        )

    # Choose a default output location if nothing was specified
    if out_path == DEFAULT_DOMAIN_PATH:
        suffix = DEFAULT_DOMAIN_PATH if migrate_file_only else "new_domain"
        out_path = domain_parent_dir / suffix

    # Ensure the output location is not already in-use
    if not migrate_file_only:
        if out_path.is_dir() and any(out_path.iterdir()):
            raise RasaException(
                f"The domain from '{domain_path}' could not be migrated to "
                f"{out_path} because that folder is not empty."
                "Please remove the folder and try again."
            )
    else:
        if out_path.is_file():
            raise RasaException(
                f"The domain from '{domain_path}' could not be migrated to "
                f"{out_path} because a file already exists."
                "Please remove the file and try again."
            )

    # Sanity Check: Assert the files to be migrated aren't in 3.0 format already
    # Note: we do not enforce that the version tag is 2.0 everywhere + validate that
    # migrate-able domain files are among these files later
    original_files = (
        [file for file in domain_path.iterdir() if Domain.is_domain_file(file)]
        if domain_path.is_dir()
        else [domain_path]
    )
    migrated_files = [
        file
        for file in original_files
        if rasa.shared.utils.io.read_yaml_file(file).get("version") == "3.0"
    ]
    if migrated_files:
        raise RasaException(
            f"Some of the given files ({[file for file in migrated_files]}) "
            f"have already been migrated to Rasa 3.0 format. Please remove these "
            f"migrated files (or replace them with files in 2.0 format) and try again."
        )

    # Validate given domain file(s) and migrate them
    try:
        created_out_dir = False
        if not migrate_file_only:
            if not out_path.is_dir():
                rasa.shared.utils.io.raise_warning(
                    f"The out path provided did not exist yet. Created directory "
                    f"{out_path}."
                )
                out_path.mkdir(parents=True)
                created_out_dir = True
            backup_location.mkdir()
            original_domain = _migrate_domain_files(
                domain_path, backup_location, out_path
            )
        else:
            if not Domain.is_domain_file(domain_path):
                raise RasaException(
                    f"The file '{domain_path}' could not be validated as a "
                    f"domain file. Only domain yaml files can be migrated. "
                )
            original_domain = _create_back_up(domain_path, backup_location)

        new_forms, updated_slots = _migrate_form_slots(original_domain)
        new_slots = _migrate_auto_fill_and_custom_slots(original_domain, updated_slots)

        _write_final_domain(domain_path, new_forms, new_slots, out_path)

        rasa.shared.utils.cli.print_success(
            f"Your domain file '{str(domain_path)}' was successfully migrated! "
            f"The migrated version is now '{str(out_path)}'. "
            f"The original domain file is backed-up at '{str(backup_location)}'."
        )

    except Exception as e:
        # Remove the backups if migration couldn't be completed
        if backup_location.is_dir():
            shutil.rmtree(backup_location)
        if out_path.is_dir():
            if created_out_dir:
                shutil.rmtree(out_path)
            else:  # just remove contained files so we do not mess with access rights
                for f in out_path.glob("*"):
                    f.unlink()
        if backup_location.is_file():
            backup_location.unlink()
        raise e
