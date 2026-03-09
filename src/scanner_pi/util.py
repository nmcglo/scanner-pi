

# monkeypatch function to change the name of the sane.SaneDev.scan property
# to avoid collision with a sane.SaneDev.options["scan"] that some devices have for button detection
def monkey_patch_sane_scan(sane_module: object) -> None:

    if not hasattr(sane_module.SaneDev, "scan"):
        raise RuntimeError("sane.SaneDev does not have a 'scan' property to monkeypatch")

    # rename the existing 'scan' property to '_scan'
    sane_module.SaneDev._scan = sane_module.SaneDev.scan
    del sane_module.SaneDev.scan