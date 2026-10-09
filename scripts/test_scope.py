"""Explicit portable files; other public files exercise Windows contracts."""
CORE_FILES = frozenset({
    "tests/clean/test_nml_writer.py",
    "tests/contracts/test_legacy_reader.py",
    "tests/contracts/test_key_converter.py",
    "tests/contracts/test_library_models.py",
    "tests/contracts/test_metadata_normalization.py",
    "tests/contracts/test_virtual_nml_catalog.py",
})

CORE_MODULES = (
    "models", "library_models", "library_access", "library_candidates",
    "library_format", "library_provider", "library_provider_registry",
    "library_selection", "legacy_reader", "legacy_compatibility",
    "metadata_normalization", "key_converter", "library_projection",
    "virtual_projection", "virtual_projection_builder", "volume_access",
    "virtual_nml_catalog", "nml_writer",
)
