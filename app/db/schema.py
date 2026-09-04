"""The schema, in the house style.

PascalCase singular tables; every entity table carries ``CreatedDate`` /
``UpdatedDate`` / ``IsDeleted`` where rows live long enough to need them.
The DDL below is SQLite; the types were chosen so the same statements port to
PostgreSQL with mechanical substitutions only (AUTOINCREMENT -> IDENTITY,
TEXT timestamps -> TIMESTAMPTZ).

This panel manages the one SoftEther server on the machine it runs on, so
there is no server table: the connection facts (management host, port, the
encrypted administrator password) live in ``Setting``. Traffic samples are
**append-only**: the panel snapshots SoftEther's cumulative per-user and
per-hub byte counters on a schedule and never updates a row; usage over a
window is *derived* from the samples, exactly the way a ledger balance is
derived. Traffic quotas are the one running total in the schema, and for a
stated reason: see ``TrafficQuota`` below.

:func:`migrate` carries a database forward from the earlier multi-server
layout: the first registered server's connection moves into the settings, and
the sample tables -- disposable time series -- are recreated in the new shape.
"""
from __future__ import annotations

TABLES: list[str] = [
    # --- panel accounts -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "PanelUser" (
        "UserID"        INTEGER PRIMARY KEY AUTOINCREMENT,
        "Username"      TEXT    NOT NULL UNIQUE,
        "PasswordHash"  TEXT    NOT NULL,
        "CreatedDate"   TEXT    NOT NULL,
        "UpdatedDate"   TEXT    NOT NULL,
        "IsDeleted"     INTEGER NOT NULL DEFAULT 0
    )
    """,
    # --- traffic samples (append-only) ----------------------------------------
    # Cumulative counters as SoftEther reports them. Bytes are the sum of the
    # unicast and broadcast counters at the moment of sampling.
    """
    CREATE TABLE IF NOT EXISTS "UserTrafficSample" (
        "UserTrafficSampleID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "HubName"             TEXT    NOT NULL,
        "UserName"            TEXT    NOT NULL,
        "SendBytes"           INTEGER NOT NULL DEFAULT 0,
        "RecvBytes"           INTEGER NOT NULL DEFAULT 0,
        "NumLogin"            INTEGER NOT NULL DEFAULT 0,
        "SampledDate"         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "HubTrafficSample" (
        "HubTrafficSampleID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "HubName"            TEXT    NOT NULL,
        "SendBytes"          INTEGER NOT NULL DEFAULT 0,
        "RecvBytes"          INTEGER NOT NULL DEFAULT 0,
        "NumSessions"        INTEGER NOT NULL DEFAULT 0,
        "NumUsers"           INTEGER NOT NULL DEFAULT 0,
        "SampledDate"        TEXT    NOT NULL
    )
    """,
    # --- VPN session history -----------------------------------------------------
    # One row per login: who, from what IP, when it started and (once the
    # session drops out of EnumSession) when it ended, and the bytes it moved
    # (SoftEther's own running total for the session, Send+Recv combined).
    # "SessionKey" is the session's UniqueId_bin, base64 -- stable for the
    # life of the connection, which "Name_str" is not guaranteed to be.
    """
    CREATE TABLE IF NOT EXISTS "VpnSessionSample" (
        "VpnSessionSampleID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "HubName"          TEXT NOT NULL,
        "UserName"         TEXT NOT NULL,
        "SessionKey"       TEXT NOT NULL,
        "ClientIP"         TEXT NOT NULL DEFAULT '',
        "ClientHostname"   TEXT NOT NULL DEFAULT '',
        "StartedDate"      TEXT NOT NULL,
        "EndedDate"        TEXT,
        "LastSeenDate"     TEXT NOT NULL,
        "BytesTotal"       INTEGER NOT NULL DEFAULT 0,
        "PacketsTotal"     INTEGER NOT NULL DEFAULT 0,
        "DownloadBytes"    INTEGER NOT NULL DEFAULT 0,
        "UploadBytes"      INTEGER NOT NULL DEFAULT 0,
        UNIQUE ("HubName", "SessionKey")
    )
    """,
    # --- per-session traffic (append-only) ---------------------------------------
    # One row per session per sampler tick, holding the session's cumulative
    # counters at that moment. Named for what they mean to the person using
    # the VPN -- SoftEther reports them from the server's side, where "sent"
    # is the client's download. Usage over a window is derived from the
    # deltas, exactly as the hub and user charts are.
    """
    CREATE TABLE IF NOT EXISTS "VpnSessionTrafficSample" (
        "VpnSessionTrafficSampleID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "VpnSessionSampleID" INTEGER NOT NULL,
        "DownloadBytes"      INTEGER NOT NULL DEFAULT 0,
        "UploadBytes"        INTEGER NOT NULL DEFAULT 0,
        "TotalBytes"         INTEGER NOT NULL DEFAULT 0,
        "SampledDate"        TEXT    NOT NULL
    )
    """,
    # --- VPN user credentials ---------------------------------------------------
    # SoftEther's own per-user hash (SHA-0(password + UPPER(username)), base64)
    # -- the same value the server stores in vpn_server.config -- cached so a
    # .vpn file can embed the credential without asking for the plaintext.
    """
    CREATE TABLE IF NOT EXISTS "VpnUserCredential" (
        "VpnUserCredentialID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "HubName"             TEXT NOT NULL,
        "UserName"            TEXT NOT NULL,
        "PasswordHash"        TEXT NOT NULL,
        "UpdatedDate"         TEXT NOT NULL,
        UNIQUE ("HubName", "UserName")
    )
    """,
    # --- traffic quotas -----------------------------------------------------
    # A byte ceiling on a Virtual Hub, or on one user's config. The consumed
    # figures are a running total the sampler advances from SoftEther's
    # cumulative counters, not a query over the samples: a quota has to
    # survive both the retention window pruning old rows and the VPN server
    # restarting its counters, and a balance carried forward does. Only
    # "LastSendBytes"/"LastRecvBytes" -- the reading the totals were last
    # advanced to -- make that arithmetic idempotent, so absorbing the same
    # reading twice adds nothing.
    #
    # "UserKey" is the case-folded name; SoftEther matches usernames without
    # case, so it is what the uniqueness and every lookup go through, while
    # "UserName" keeps the spelling the operator typed.
    """
    CREATE TABLE IF NOT EXISTS "TrafficQuota" (
        "TrafficQuotaID" INTEGER PRIMARY KEY AUTOINCREMENT,
        "SubjectType"    TEXT    NOT NULL,
        "HubName"        TEXT    NOT NULL,
        "UserName"       TEXT    NOT NULL DEFAULT '',
        "UserKey"        TEXT    NOT NULL DEFAULT '',
        "LimitBytes"     INTEGER NOT NULL DEFAULT 0,
        "Metric"         TEXT    NOT NULL DEFAULT 'total',
        "IsEnabled"      INTEGER NOT NULL DEFAULT 1,
        "UploadBytes"    INTEGER NOT NULL DEFAULT 0,
        "DownloadBytes"  INTEGER NOT NULL DEFAULT 0,
        "LastSendBytes"  INTEGER NOT NULL DEFAULT -1,
        "LastRecvBytes"  INTEGER NOT NULL DEFAULT -1,
        "CycleStartDate" TEXT    NOT NULL,
        "ExceededDate"   TEXT,
        "EnforcedDate"   TEXT,
        "RestoreState"   TEXT    NOT NULL DEFAULT '',
        "CreatedDate"    TEXT    NOT NULL,
        "UpdatedDate"    TEXT    NOT NULL,
        UNIQUE ("SubjectType", "HubName", "UserKey")
    )
    """,
    # --- panel settings and audit ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS "Setting" (
        "SettingKey"   TEXT PRIMARY KEY,
        "SettingValue" TEXT NOT NULL,
        "UpdatedDate"  TEXT NOT NULL
    )
    """,
    # Who did what, from the panel's point of view. The action vocabulary is
    # open-ended (every endpoint that changes something writes one), so it is
    # a text column rather than a lookup.
    """
    CREATE TABLE IF NOT EXISTS "AuditLog" (
        "AuditLogID"  INTEGER PRIMARY KEY AUTOINCREMENT,
        "UserID"      INTEGER REFERENCES "PanelUser"("UserID"),
        "Username"    TEXT    NOT NULL,
        "Action"      TEXT    NOT NULL,
        "TargetType"  TEXT    NOT NULL DEFAULT '',
        "TargetKey"   TEXT    NOT NULL DEFAULT '',
        "Detail"      TEXT    NOT NULL DEFAULT '',
        "CreatedDate" TEXT    NOT NULL
    )
    """,
]

INDEXES: list[str] = [
    'CREATE INDEX IF NOT EXISTS "IX_UserTrafficSample_Key" '
    'ON "UserTrafficSample"("HubName", "UserName", "SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_UserTrafficSample_Date" ON "UserTrafficSample"("SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_HubTrafficSample_Key" '
    'ON "HubTrafficSample"("HubName", "SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_HubTrafficSample_Date" ON "HubTrafficSample"("SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_AuditLog_Date" ON "AuditLog"("CreatedDate")',
    'CREATE INDEX IF NOT EXISTS "IX_VpnSessionSample_Open" '
    'ON "VpnSessionSample"("HubName", "EndedDate")',
    'CREATE INDEX IF NOT EXISTS "IX_VpnSessionSample_User" '
    'ON "VpnSessionSample"("HubName", "UserName", "StartedDate")',
    'CREATE INDEX IF NOT EXISTS "IX_VpnSessionTrafficSample_Session" '
    'ON "VpnSessionTrafficSample"("VpnSessionSampleID", "SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_VpnSessionTrafficSample_Date" '
    'ON "VpnSessionTrafficSample"("SampledDate")',
    'CREATE INDEX IF NOT EXISTS "IX_TrafficQuota_Hub" '
    'ON "TrafficQuota"("HubName", "SubjectType")',
]

SEEDS: list[tuple[str, list[dict]]] = []

#: Tables from the earlier multi-server design, kept out of the schema and
#: removed by the migration once anything worth keeping has been carried over.
LEGACY_TABLES = ["InstallLog", "InstallStep", "InstallJob", "InstallStepStatus", "InstallJobStatus", "Server"]


def migrate(conn) -> None:
    """Carry a pre-single-server database forward. Idempotent.

    Runs inside init_schema's transaction, before the CREATE statements, so
    the old sample tables (which carried a ServerID column) can be dropped
    and recreated in the new shape.
    """
    def table_exists(name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def has_column(table: str, column: str) -> bool:
        return any(r[1] == column for r in conn.execute(f'PRAGMA table_info("{table}")'))

    # 1. Adopt the first registered server's connection into the settings, so
    #    an installation that managed one remote server keeps managing it.
    if table_exists("Server") and table_exists("Setting"):
        already = conn.execute(
            "SELECT 1 FROM \"Setting\" WHERE \"SettingKey\" = 'se_password'"
        ).fetchone()
        if not already:
            row = conn.execute(
                'SELECT "Host", "Port", "EncryptedPassword" FROM "Server" '
                'WHERE "IsDeleted" = 0 ORDER BY "ServerID" LIMIT 1'
            ).fetchone()
            if row:
                import json

                now = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).replace(microsecond=0).isoformat()
                for key, value in (
                    ("se_host", json.dumps(row[0])),
                    ("se_port", json.dumps(int(row[1]))),
                    # Already encrypted with this installation's key; stored verbatim.
                    ("se_password", json.dumps(row[2])),
                ):
                    conn.execute(
                        'INSERT INTO "Setting"("SettingKey", "SettingValue", "UpdatedDate") '
                        "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                        (key, value, now),
                    )

    # 1b. Session rows gained per-direction totals once the per-session
    #     traffic series arrived; existing rows keep zeroes until sampled.
    if table_exists("VpnSessionSample"):
        for column in ("DownloadBytes", "UploadBytes"):
            if not has_column("VpnSessionSample", column):
                conn.execute(
                    f'ALTER TABLE "VpnSessionSample" ADD COLUMN "{column}" INTEGER NOT NULL DEFAULT 0'
                )

    # 1c. The per-session series gained the combined counter: SoftEther
    #     reports no direction split for sessions with no client transport
    #     of their own, and a chart still has to have something to draw.
    if table_exists("VpnSessionTrafficSample") and not has_column(
        "VpnSessionTrafficSample", "TotalBytes"
    ):
        conn.execute(
            'ALTER TABLE "VpnSessionTrafficSample" ADD COLUMN "TotalBytes" INTEGER NOT NULL DEFAULT 0'
        )

    # 2. The sample tables are disposable time series; the old shape carried a
    #    ServerID column. Recreate rather than alter.
    for table in ("UserTrafficSample", "HubTrafficSample"):
        if table_exists(table) and has_column(table, "ServerID"):
            conn.execute(f'DROP TABLE "{table}"')

    # 3. Drop what the multi-server design owned.
    for table in LEGACY_TABLES:
        if table_exists(table):
            conn.execute(f'DROP TABLE "{table}"')
