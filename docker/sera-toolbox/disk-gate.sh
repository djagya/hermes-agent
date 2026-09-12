#!/bin/sh
# Release B disk gate (plan 5c). Called from stage2 before migration.
# No PRAGMA quick_check (15 GB state.db). Openability is SELECT 1
# on existing *.db files only. Never walks cache/model trees.
set -eu

file_bytes() {
    [ -f "$1" ] || { echo 0; return 0; }
    if stat -c %s "$1" >/dev/null 2>&1; then
        stat -c %s "$1"
    else
        stat -f %z "$1"
    fi
}

add_sqlite_bytes() {
    [ -e "$1" ] || return 0
    if [ -f "$1" ]; then
        sqlite_bytes=$((sqlite_bytes + $(file_bytes "$1")))
        return 0
    fi
    extra=0
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        extra=$((extra + $(file_bytes "$f")))
    done <<EOF
$(find "$1" \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) -type f 2>/dev/null)
EOF
    sqlite_bytes=$((sqlite_bytes + extra))
}

need_backup_kb() {
    echo $(($1 * 3 / 1024))
}

disk_gate_check() {
    home="$1"
    min_free_kb="${HERMES_MIN_FREE_KB:-2097152}"
    avail_kb="${HERMES_DISK_GATE_AVAIL_KB:-}"
    if [ -z "$avail_kb" ]; then
        avail_kb="$(df -Pk "$home" 2>/dev/null | awk 'NR==2 {print $4}')"
    fi
    if [ -z "${avail_kb:-}" ]; then
        echo "[disk-gate] ERROR: cannot measure free space on $home" >&2
        return 1
    fi
    if [ "$avail_kb" -lt "$min_free_kb" ]; then
        echo "[disk-gate] ERROR: $home has ${avail_kb}KB free; need ${min_free_kb}KB" >&2
        return 1
    fi
    avail_inodes="${HERMES_DISK_GATE_AVAIL_INODES:-}"
    if [ -z "$avail_inodes" ]; then
        avail_inodes="$(df -Pi "$home" 2>/dev/null | awk 'NR==2 {print $4}')"
    fi
    if [ -z "${avail_inodes:-}" ] || [ "$avail_inodes" -lt "${HERMES_MIN_FREE_INODES:-10000}" ]; then
        echo "[disk-gate] ERROR: $home has ${avail_inodes:-0} inodes free" >&2
        return 1
    fi
    tmp_min_kb="${HERMES_MIN_TMP_FREE_KB:-262144}"
    tmp_avail_kb="${HERMES_DISK_GATE_TMP_AVAIL_KB:-}"
    if [ -z "$tmp_avail_kb" ]; then
        tmp_avail_kb="$(df -Pk /tmp 2>/dev/null | awk 'NR==2 {print $4}')"
    fi
    if [ -z "${tmp_avail_kb:-}" ] || [ "$tmp_avail_kb" -lt "$tmp_min_kb" ]; then
        echo "[disk-gate] ERROR: /tmp has ${tmp_avail_kb:-0}KB free; need ${tmp_min_kb}KB" >&2
        return 1
    fi

    sqlite_bytes=0
    add_sqlite_bytes "$home/state.db"
    add_sqlite_bytes "$home/state.db-wal"
    add_sqlite_bytes "$home/state.db-shm"
    add_sqlite_bytes "$home/kanban.db"
    add_sqlite_bytes "$home/kanban.db-wal"
    add_sqlite_bytes "$home/kanban.db-shm"
    add_sqlite_bytes "$home/observatory.db"
    add_sqlite_bytes "$home/state/hermes-org-observability/observatory.db"
    add_sqlite_bytes "$home/cron"
    add_sqlite_bytes "$home/kanban"
    add_sqlite_bytes "$home/profiles"
    need_kb="$(need_backup_kb "$sqlite_bytes")"
    if [ "$need_kb" -gt 0 ] && [ "$avail_kb" -lt "$need_kb" ]; then
        echo "[disk-gate] ERROR: $home has ${avail_kb}KB free; need ${need_kb}KB (3× SQLite+WAL=${sqlite_bytes}B)" >&2
        return 1
    fi
    return 0
}

# Fail-closed if a required DB exists but cannot be opened. Missing
# files are OK (fresh home). WAL/SHM are not opened. No repair.
open_sqlite() {
    db="$1"
    [ -f "$db" ] || return 0
    case "$db" in
        *.db-wal|*.db-shm) return 0 ;;
    esac
    if ! command -v sqlite3 >/dev/null 2>&1; then
        echo "[disk-gate] ERROR: sqlite3 missing; cannot preflight $db" >&2
        return 1
    fi
    # Empty file = not initialized yet (docker-exec touch / first
    # boot). Hermes creates the schema later. Non-empty without the
    # magic header is corrupt.
    sz="$(file_bytes "$db")"
    if [ "$sz" = 0 ]; then
        return 0
    fi
    # Some sqlite3 CLIs initialize a tiny/invalid file as a new DB
    # (exit 0) and would mutate a corrupt live file. Require the
    # magic header before SELECT 1.
    hdr="$(dd if="$db" bs=16 count=1 2>/dev/null || true)"
    case "$hdr" in
        "SQLite format 3"*) ;;
        *)
            echo "[disk-gate] ERROR: cannot open SQLite $db" >&2
            return 1
            ;;
    esac
    if ! sqlite3 "$db" "SELECT 1;" >/dev/null 2>&1; then
        echo "[disk-gate] ERROR: cannot open SQLite $db" >&2
        return 1
    fi
    return 0
}

db_open_check() {
    home="$1"
    open_sqlite "$home/state.db" || return 1
    open_sqlite "$home/kanban.db" || return 1
    open_sqlite "$home/observatory.db" || return 1
    open_sqlite "$home/state/hermes-org-observability/observatory.db" || return 1
    for root in "$home/cron" "$home/kanban" "$home/profiles"; do
        [ -d "$root" ] || continue
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            open_sqlite "$f" || return 1
        done <<EOF
$(find "$root" -name '*.db' -type f 2>/dev/null)
EOF
    done
    return 0
}

self_test() {
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    # 4096 bytes → 3× = 12 KB
    dd if=/dev/zero of="$tmp/state.db" bs=1024 count=4 >/dev/null 2>&1
    HERMES_MIN_FREE_KB=1 HERMES_MIN_FREE_INODES=1 HERMES_MIN_TMP_FREE_KB=1 \
        HERMES_DISK_GATE_AVAIL_KB=100 HERMES_DISK_GATE_AVAIL_INODES=100 \
        HERMES_DISK_GATE_TMP_AVAIL_KB=100 \
        disk_gate_check "$tmp" || {
        echo "self-test: expected pass on 12KB need / 100KB free" >&2
        return 1
    }
    HERMES_MIN_FREE_KB=50 HERMES_MIN_FREE_INODES=1 HERMES_MIN_TMP_FREE_KB=1 \
        HERMES_DISK_GATE_AVAIL_KB=10 HERMES_DISK_GATE_AVAIL_INODES=100 \
        HERMES_DISK_GATE_TMP_AVAIL_KB=100 \
        disk_gate_check "$tmp" 2>/dev/null && {
        echo "self-test: expected HERMES_MIN_FREE fail" >&2
        return 1
    }
    HERMES_MIN_FREE_KB=1 HERMES_MIN_FREE_INODES=1 HERMES_MIN_TMP_FREE_KB=1 \
        HERMES_DISK_GATE_AVAIL_KB=11 HERMES_DISK_GATE_AVAIL_INODES=100 \
        HERMES_DISK_GATE_TMP_AVAIL_KB=100 \
        disk_gate_check "$tmp" 2>/dev/null && {
        echo "self-test: expected 3x WAL fail" >&2
        return 1
    }
    [ "$(need_backup_kb 4096)" = 12 ] || {
        echo "self-test: need_backup_kb 4096 != 12" >&2
        return 1
    }
    if ! command -v sqlite3 >/dev/null 2>&1; then
        echo "self-test: sqlite3 missing" >&2
        return 1
    fi
    rm -f "$tmp/state.db"
    sqlite3 "$tmp/state.db" "CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1);"
    db_open_check "$tmp" || {
        echo "self-test: expected open pass on valid state.db" >&2
        return 1
    }
    rm -f "$tmp/state.db-wal" "$tmp/state.db-shm"
    printf 'not-a-db\n' > "$tmp/state.db"
    if db_open_check "$tmp" 2>/dev/null; then
        echo "self-test: expected open fail on garbage state.db" >&2
        return 1
    fi
    rm -f "$tmp/state.db"
    db_open_check "$tmp" || {
        echo "self-test: expected open pass when state.db missing" >&2
        return 1
    }
    : > "$tmp/state.db"
    db_open_check "$tmp" || {
        echo "self-test: expected open pass on empty state.db" >&2
        return 1
    }
    rm -f "$tmp/state.db"
    dd if=/dev/zero of="$tmp/state.db" bs=1024 count=4 >/dev/null 2>&1
    mkdir -p "$tmp/profiles/stay"
    dd if=/dev/zero of="$tmp/profiles/stay/state.db" bs=1024 count=4 >/dev/null 2>&1
    sqlite_bytes=0
    add_sqlite_bytes "$tmp/state.db"
    add_sqlite_bytes "$tmp/profiles"
    [ "$sqlite_bytes" = 8192 ] || {
        echo "self-test: profiles walk got $sqlite_bytes want 8192" >&2
        return 1
    }
    return 0
}

case "${1:-}" in
    --self-test)
        self_test
        ;;
    --check)
        disk_gate_check "${2:?home}" || exit 1
        db_open_check "$2"
        ;;
    "")
        echo "usage: disk-gate.sh --check HOME | --self-test" >&2
        exit 2
        ;;
    *)
        disk_gate_check "$1" || exit 1
        db_open_check "$1"
        ;;
esac
