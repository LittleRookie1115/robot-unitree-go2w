# Vendored third-party snapshots

`unitree_sdk2` and `unitree_ros2` are built by the native profile. The mapping
and navigation trees are retained source snapshots; `robonix_manifest.yaml`
does not launch them.

They are stored as normal directories. Root `.gitmodules` is not used.
`third_party/PINS` records the upstream repository and the Git tree id of the
copy committed here. `scripts/verify_submodule_pins.sh` checks those tree ids
and fails if a directory becomes a gitlink or its content changes without a
new pin.
