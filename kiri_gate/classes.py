from enum import Enum


class Class(Enum):
    """What a tool can break. Fixed when the tool is registered."""
    READ = "read"                   # looks, changes nothing
    UNDOABLE = "undoable"           # changes something you can roll back
    IRREVERSIBLE = "irreversible"   # deletes, runs commands, can't be cleanly rolled back
    EXTERNAL = "external"           # leaves the machine: money, messages, production, other people


READ, UNDOABLE, IRREVERSIBLE, EXTERNAL = Class.READ, Class.UNDOABLE, Class.IRREVERSIBLE, Class.EXTERNAL


def always_asks(c: Class) -> bool:
    return c in (Class.IRREVERSIBLE, Class.EXTERNAL)


def parse(name: str) -> Class:
    try:
        return Class(str(name).strip().lower())
    except ValueError:
        raise ValueError(f"unknown class '{name}': use read, undoable, irreversible or external")
