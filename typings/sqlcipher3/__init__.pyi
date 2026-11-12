# Minimal stub: sqlcipher3 mirrors the stdlib sqlite3 DB-API exactly, but ships no type info.
# The runtime classes are sqlcipher3's own; typing them as sqlite3's is accurate for usage.
from sqlite3 import Connection as Connection
from sqlite3 import DatabaseError as DatabaseError
from sqlite3 import Error as Error
from sqlite3 import IntegrityError as IntegrityError
from sqlite3 import OperationalError as OperationalError

def connect(
    database: str,
    timeout: float = 5.0,
    detect_types: int = 0,
    isolation_level: str | None = "DEFERRED",
    check_same_thread: bool = True,
    cached_statements: int = 128,
    uri: bool = False,
) -> Connection: ...
