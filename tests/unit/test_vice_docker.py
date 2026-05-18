"""Unit tests for defmon_driver.vice_docker — covers the pure-python
``x64sc_args()`` command-line builder and the ``DiskMount.docker_arg()``
helper. Docker is never invoked."""

from __future__ import annotations

from defmon_driver.vice_docker import DiskMount, ViceContainer


def test_x64sc_args_defaults() -> None:
    args = ViceContainer().x64sc_args()
    assert "-default" in args
    assert "-binarymonitor" in args
    assert "-binarymonitoraddress" in args
    assert "ip4://0.0.0.0:6502" in args
    assert "-sounddev" in args
    assert args[args.index("-sounddev") + 1] == "dummy"
    # warp=True is the default
    assert "-warp" in args
    # No autostart / silent / sid extras by default
    assert "-autostart" not in args
    assert "-silent" not in args
    assert "-sidextra" not in args
    assert "+drive8truedrive" not in args  # truedrive=True is default


def test_x64sc_args_autostart_path_threaded_through() -> None:
    args = ViceContainer(autostart="/work/foo.d64").x64sc_args()
    assert args[args.index("-autostart") + 1] == "/work/foo.d64"


def test_x64sc_args_silent_and_no_warp() -> None:
    args = ViceContainer(warp=False, silent=True).x64sc_args()
    assert "-warp" not in args
    assert "-silent" in args


def test_x64sc_args_disables_truedrive_when_requested() -> None:
    args = ViceContainer(truedrive=False).x64sc_args()
    assert "+drive8truedrive" in args


def test_x64sc_args_sound_dump_passes_path_via_soundarg() -> None:
    args = ViceContainer(
        sounddev="dump",
        sounddump_path="/work/sound.dump",
    ).x64sc_args()
    assert args[args.index("-sounddev") + 1] == "dump"
    assert "-soundarg" in args
    assert args[args.index("-soundarg") + 1] == "/work/sound.dump"


def test_x64sc_args_sound_dump_without_path_omits_soundarg() -> None:
    # Missing sounddump_path should NOT inject a None or crash.
    args = ViceContainer(sounddev="dump").x64sc_args()
    assert "-soundarg" not in args


def test_x64sc_args_2sid_emits_sid2_address_only() -> None:
    args = ViceContainer(sid_extras=1, sid2_address=0xD420).x64sc_args()
    assert "-sidextra" in args
    assert args[args.index("-sidextra") + 1] == "1"
    assert "-sid2address" in args
    assert args[args.index("-sid2address") + 1] == "0xd420"
    assert "-sid3address" not in args


def test_x64sc_args_3sid_emits_sid3_address() -> None:
    args = ViceContainer(sid_extras=2, sid2_address=0xD420, sid3_address=0xD440).x64sc_args()
    assert args[args.index("-sidextra") + 1] == "2"
    assert args[args.index("-sid3address") + 1] == "0xd440"


def test_x64sc_args_extra_args_appended() -> None:
    args = ViceContainer(extra_args=["-myextra", "1"]).x64sc_args()
    # Extras come after the rest.
    assert args[-2:] == ["-myextra", "1"]


def test_x64sc_args_extra_args_after_autostart() -> None:
    # Both autostart and extra_args set: extra_args still trail autostart.
    args = ViceContainer(autostart="/work/a.d64", extra_args=["-x", "1"]).x64sc_args()
    autostart_i = args.index("-autostart")
    extras_i = args.index("-x")
    assert extras_i > autostart_i


def test_disk_mount_docker_arg_readonly() -> None:
    m = DiskMount("/host/path.d64", "/work/path.d64", read_only=True)
    flag = m.docker_arg()
    assert flag[0] == "-v"
    # second element is "<abs-host>:/work/path.d64:ro"
    spec = flag[1]
    assert spec.endswith(":/work/path.d64:ro")


def test_disk_mount_docker_arg_writable_default() -> None:
    m = DiskMount("/host/p.d64", "/work/p.d64")
    spec = m.docker_arg()[1]
    assert spec.endswith(":/work/p.d64:rw")


def test_vice_container_generates_unique_name_when_unset() -> None:
    a = ViceContainer()
    b = ViceContainer()
    assert a.name is not None
    assert b.name is not None
    # Two distinct constructions should produce two distinct names.
    assert a.name != b.name


def test_vice_container_name_respected_when_set() -> None:
    c = ViceContainer(name="my-container")
    assert c.name == "my-container"
