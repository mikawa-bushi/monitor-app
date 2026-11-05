#!/bin/sh
# monitor-app コンテナのエントリポイント(POSIX sh)。
#
# 1. root で起動されたら /project の所有者(uid:gid)に切り替えて、このスクリプトを
#    実行し直す。ホストのフォルダをマウントしたとき、作られる DB などのファイルの所有者が
#    ホストの利用者になる(uid 1000 以外でも設定なしで動く)。--user で起動されたときは
#    そのまま動く。切り替えるときは権限(capability)をすべて捨て、setuid のプログラムでも
#    権限を得られないようにする(no_new_privs)。
#    - 所有者が root(rootless Docker / Podman / Docker Desktop。rootful Docker で
#      -v に存在しないフォルダを指定して Docker が作った場合も)なら root のまま動かすが、
#      権限は同じように捨てる(root の所有のファイルには所有者として書き込める)。
#      rootful Docker と見られるときは、フォルダの所有者を直す方法を表示する。
#    - 切り替えられない(--cap-drop ALL で SETUID・SETGID が無い)とき、/project に
#      書き込めなければ必要な権限を示して終了(1)、書き込めれば警告して root のまま続ける。
# 2. runserver のときだけプロジェクトを確かめる(--help のときは確かめない)。
#    - /project をマウントしておらず(空のまま)config.py も無い → 物流倉庫の在庫画面の
#      デモ(monitor-app demo)を起動する。データは一時的で、何も書き込まない
#      (docker run だけで動く画面が出る。runserver の --host / --port はそのまま渡す)
#    - 設定ファイル(--config、既定 config.py)が無く、マウントした /project が空
#      → サンプルのプロジェクトを作る(手元に残せる雛形)
#    - config.py が無く空でもない   → 説明を表示して終了(1)
#    - SQLite の保存先に書き込めない → 対処を表示して終了(1)
# 3. コマンドを exec する(PID 1 として docker stop の SIGTERM を直接受け取る)。
#
# 引数の扱い:
#   docker run <image>                 → monitor-app runserver --import-csv-if-empty
#                                        (マウントしていなければ monitor-app demo)
#   docker run <image> --version       → monitor-app --version(-V も同じ)
#   docker run <image> --import-csv    → monitor-app runserver --import-csv-if-empty --import-csv
#   docker run <image> check           → monitor-app check(コマンドとして無い名前は
#                                        monitor-app のサブコマンドとみなす)
#   docker run <image> sh              → sh
set -eu

PROJECT_DIR=${MONITOR_PROJECT_DIR:-/project}
DEMO_DIR=${MONITOR_DEMO_DIR:-/opt/monitor-app/demo}

# --version(-V)だけならバージョンを表示する。オプションだけ渡されたら runserver に
# 渡す。コマンドとして無い名前(check など)は monitor-app のサブコマンドとして実行する。
if [ "$#" -eq 1 ] && { [ "$1" = --version ] || [ "$1" = -V ]; }; then
    set -- monitor-app --version
elif [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ]; then
    set -- monitor-app runserver --import-csv-if-empty "$@"
elif ! command -v "$1" >/dev/null 2>&1; then
    set -- monitor-app "$@"
fi

die() {
    for line in "$@"; do
        printf '%s\n' "$line" >&2
    done
    exit 1
}

# フォルダが空か(ext4 のボリュームが最初から持つ lost+found は数えない)。
is_empty() {
    for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
        # 何も一致しなければパターンがそのまま残る(存在しない名前として飛ばす)。
        if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
            continue
        fi
        if [ "${entry##*/}" != lost+found ]; then
            return 1
        fi
    done
    return 0
}

# setpriv に付ける権限の制限(強い順)。権限(capability)の境界(bounding set)を空に
# するには CAP_SETPCAP が要り、--cap-drop ALL などでは使えないため、使えるものを選ぶ。
harden_flags() {
    case $1 in
        1) echo "--bounding-set=-all --inh-caps=-all --no-new-privs" ;;
        2) echo "--inh-caps=-all --no-new-privs" ;;
        *) echo "" ;;
    esac
}

# コンテナの root がホストの root と別のユーザーか(rootless Docker・Podman の
# ユーザー名前空間)。uid_map が「0 0 4294967295」なら同じ(rootful)。
in_user_namespace() {
    map=$(cat "${MONITOR_TEST_UID_MAP:-/proc/self/uid_map}" 2>/dev/null) || return 1
    # shellcheck disable=SC2086 # 空白で区切られた 3 つの数を位置引数にする
    set -- $map
    [ "$#" -ge 3 ] || return 1
    [ "$1 $2 $3" != "0 0 4294967295" ]
}

# Docker Desktop(Windows・macOS)の仮想マシンか(フォルダの共有ではファイルが root の
# 所有に見えるが、ホストの利用者の権限で書き込まれる)。
docker_desktop() {
    case $(uname -r 2>/dev/null) in
        *linuxkit* | *microsoft* | *WSL*) return 0 ;;
    esac
    return 1
}

# --- 1. 権限: root ならプロジェクトの所有者に切り替える ---------------------------
# 権限を捨てて実行し直した後(_MONITOR_APP_ROOT_LIMITED)は、もう一度は行わない。
if [ "$(id -u)" = 0 ] && [ -d "$PROJECT_DIR" ] \
    && [ -z "${_MONITOR_APP_ROOT_LIMITED:-}" ]; then
    owner=$(stat -c '%u:%g' "$PROJECT_DIR")
    uid=${owner%%:*}
    gid=${owner#*:}
    if [ "$uid" != 0 ]; then
        # 空の instances/ が root の所有なら(名前付きボリュームのマウント先として
        # Docker が作った場合)所有者に合わせる。中身のあるフォルダと、シンボリック
        # リンク(リンク先のコンテナ内のフォルダを渡さないため)には触れない。確かめた
        # 後にリンクへすり替えられても、-h でリンク自体だけを変える(リンク先は変えない)。
        if [ -d "$PROJECT_DIR/instances" ] && [ ! -L "$PROJECT_DIR/instances" ] \
            && is_empty "$PROJECT_DIR/instances" \
            && [ "$(stat -c '%u' "$PROJECT_DIR/instances")" = 0 ]; then
            chown -h "$owner" "$PROJECT_DIR/instances" 2>/dev/null || true
        fi
        # 切り替えられるかを先に確かめる(失敗したまま exec すると起動できない)。
        for level in 1 2 3; do
            flags=$(harden_flags "$level")
            # shellcheck disable=SC2086 # $flags は複数のオプション(空のこともある)
            if setpriv --reuid="$uid" --regid="$gid" --clear-groups $flags true 2>/dev/null; then
                # root のホーム(/root)は切り替え後のユーザーが読めないため、プロジェクトにする。
                HOME=$PROJECT_DIR
                export HOME
                # shellcheck disable=SC2086
                exec setpriv --reuid="$uid" --regid="$gid" --clear-groups $flags \
                    -- /bin/sh "$0" "$@"
            fi
        done
        if [ ! -w "$PROJECT_DIR" ]; then
            # 権限のない root は、別のユーザーの所有のフォルダに書き込めない。
            die "エラー: $PROJECT_DIR の所有者(uid=$uid)に切り替えられません(コンテナに SETUID・SETGID の権限がありません)。" \
                "  --cap-drop ALL を付けるなら --cap-add SETUID --cap-add SETGID --cap-add CHOWN も付けてください。" \
                "  または --user $uid:$gid で、フォルダの所有者として起動してください。"
        fi
        echo "monitor-app: $PROJECT_DIR の所有者(uid=$uid)に切り替えられないため、root のまま実行します(切り替えるには --cap-add SETUID --cap-add SETGID を付けてください)" >&2
    elif ! in_user_namespace && ! docker_desktop; then
        echo "monitor-app: $PROJECT_DIR の所有者が root のため、root のまま(権限を持たせずに)実行します。作られるファイルもホストで root の所有になります。-v に存在しないフォルダを指定すると、Docker が root の所有で作ります。ホストで sudo chown -R \$(id -u):\$(id -g) <フォルダ> を実行すると、次の起動からはその利用者で動きます" >&2
    fi
    # root のまま続ける: 権限(capability)を捨て、setuid のプログラムでも得られない
    # ようにしてから、このスクリプトを実行し直す(以降の確認も権限なしで行う)。
    for level in 1 2; do
        flags=$(harden_flags "$level")
        # shellcheck disable=SC2086
        if setpriv $flags true 2>/dev/null; then
            _MONITOR_APP_ROOT_LIMITED=1
            export _MONITOR_APP_ROOT_LIMITED
            # shellcheck disable=SC2086
            exec setpriv $flags -- /bin/sh "$0" "$@"
        fi
    done
fi
unset _MONITOR_APP_ROOT_LIMITED

# --- 2. プロジェクトの確認(runserver のときだけ)----------------------------------

# runserver に渡された --config の値(無ければ config.py)。
config_path() {
    file=config.py
    while [ "$#" -gt 0 ]; do
        case $1 in
            --config)
                if [ "$#" -ge 2 ]; then
                    file=$2
                fi
                ;;
            --config=*) file=${1#--config=} ;;
        esac
        shift
    done
    printf '%s\n' "$file"
}

# MONITOR_DATABASE_URL の値(環境変数、なければ .env の最後の定義、なければ既定値)。
database_url() {
    if [ -n "${MONITOR_DATABASE_URL:-}" ]; then
        printf '%s\n' "$MONITOR_DATABASE_URL"
        return
    fi
    value=""
    if [ -f .env ]; then
        value=$(sed -n \
            -e 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}MONITOR_DATABASE_URL[[:space:]]*=[[:space:]]*//p' \
            .env | tail -n 1)
        # 引用符と、引用符なしの値の後ろのコメント(" # ...")を外す。
        case $value in
            \"*) value=${value#\"}; value=${value%%\"*} ;;
            \'*) value=${value#\'}; value=${value%%\'*} ;;
            *) value=$(printf '%s' "$value" | sed -e 's/[[:space:]][[:space:]]*#.*$//' -e 's/[[:space:]]*$//') ;;
        esac
    fi
    printf '%s\n' "${value:-sqlite:///instances/database.db}"
}

# SQLite ファイルのパス(サーバー DB・メモリ上の DB なら空)。
sqlite_path() {
    case $1 in
        sqlite:///* | sqlite+*:///*) path=${1#*:///} ;;
        *) return 0 ;;
    esac
    path=${path%%\?*}
    case $path in
        '' | :memory: | file:*) return 0 ;;
    esac
    printf '%s\n' "$path"
}

# ファイル・フォルダの所有者の uid(ls -n は POSIX。GNU と BSD の stat の違いを避ける)。
owner_uid() {
    # shellcheck disable=SC2012 # 1 つのパスの所有者だけを読む(find -printf は GNU 専用)
    ls -ldn -- "$1" 2>/dev/null | awk '{print $3}'
}

# 書き込めない保存先を、原因に合わせた対処とともに表示して終了する。
# $1: 表示するパス、$2: 書き込めなかったファイル・フォルダ
unwritable() {
    case $1 in
        /*) shown=$1 ;;
        .) shown=$PROJECT_DIR ;;
        *) shown=$PROJECT_DIR/${1#./} ;;
    esac
    me=$(id -u)
    if [ "$(owner_uid "$2")" = "$me" ]; then
        # 所有者として動いているのに書けない: 読み取り専用のマウントか、権限の設定。
        hint="  読み取り専用(:ro)でマウントしていないか、フォルダに書き込みの権限(chmod u+w)があるかを確かめてください。"
    elif [ "$(owner_uid "$PROJECT_DIR")" = "$me" ]; then
        # フォルダの所有者として動いているが、中に別の所有者のもの(以前 root などで
        # 作られた DB など)がある。
        hint="  中のファイル・フォルダの所有者がフォルダと違います。ホストで sudo chown -R $me:$(id -g) <プロジェクトのフォルダ> を実行して所有者をそろえてください。"
    else
        hint="  --user を付けずに起動すると、フォルダの所有者に自動で切り替わります(または sudo chown -R <uid>:<gid> でフォルダの所有者を直す)。"
    fi
    die "エラー: SQLite の保存先 $shown に書き込めません(uid=$(id -u) gid=$(id -g) で実行中)。" \
        "$hint" \
        "  Docker Desktop(Windows/macOS)では instances/ を名前付きボリュームにする方法もあります(docker/README.md を参照)。"
}

check_database_writable() {
    db=$(sqlite_path "$(database_url)")
    [ -n "$db" ] || return 0
    dir=$(dirname -- "$db")
    # まだ無いフォルダはアプリが作る。作れるか(いちばん近い既存の親に書けるか)を見る。
    probe=$dir
    while [ ! -e "$probe" ]; do
        probe=$(dirname -- "$probe")
    done
    if [ ! -w "$probe" ]; then
        unwritable "$dir" "$probe"
    fi
    # DB と、SQLite が同じフォルダに作るファイル(WAL など)にも書き込めること。
    for file in "$db" "$db-wal" "$db-shm" "$db-journal"; do
        if [ -e "$file" ] && [ ! -w "$file" ]; then
            unwritable "$file" "$file"
        fi
    done
}

prepare_project() {
    cd "$PROJECT_DIR"
    config=$(config_path "$@")
    if [ ! -f "$config" ]; then
        if [ "$config" != config.py ]; then
            # --config で指定したファイルが無いことは runserver が知らせる。
            return 0
        fi
        if ! is_empty .; then
            die "エラー: $PROJECT_DIR に config.py がありません(フォルダが空でもないため、デモも作りません)。" \
                "  monitor-app のプロジェクト(config.py と csv/ のあるフォルダ)を /project にマウントしてください。" \
                "  例: docker run -p 9990:9990 -v \"\$PWD\":/project <イメージ>(空のフォルダをマウントするとデモを作ります)"
        fi
        if [ ! -w . ]; then
            die "エラー: $PROJECT_DIR に書き込めないため、デモプロジェクトを作れません(uid=$(id -u) で実行中)。" \
                "  --user を付けずに起動するか、書き込めるフォルダをマウントしてください。" \
                "  例: docker run -p 9990:9990 -v \"\$PWD\":/project <イメージ>"
        fi
        cp -R "$DEMO_DIR"/. .
        echo "monitor-app: 空の $PROJECT_DIR にデモプロジェクト(config.py・csv/)を作成しました" >&2
    fi
    check_database_writable
}

# ヘルプの表示か(--help ではデモを作らない)。
wants_help() {
    for arg in "$@"; do
        case $arg in
            --help | -h) return 0 ;;
        esac
    done
    return 1
}

# DIR がマウントポイントか(-v・--mount・名前付きボリューム)。mountinfo の 5 番目の欄が
# マウント先。
is_mounted() {
    awk -v dir="$1" '$5 == dir { found = 1 } END { exit !found }' \
        "${MONITOR_TEST_MOUNTINFO:-/proc/self/mountinfo}" 2>/dev/null
}

# runserver の引数が、demo でも使えるものだけか(--host・--port と、取り込みの
# --import-csv-if-empty)。
demo_accepts() {
    shift 2
    while [ "$#" -gt 0 ]; do
        case $1 in
            --import-csv-if-empty | --host=* | --port=*) ;;
            --host | --port)
                [ "$#" -ge 2 ] || return 1
                shift
                ;;
            *) return 1 ;;
        esac
        shift
    done
}

# 倉庫在庫のデモで起動するか: /project がマウントされておらず(イメージの空のフォルダの
# まま)、config.py も無い。
wants_demo() {
    [ ! -e "$PROJECT_DIR/config.py" ] && is_empty "$PROJECT_DIR" \
        && ! is_mounted "$PROJECT_DIR" && demo_accepts "$@"
}

case ${1:-} in
    monitor-app | */monitor-app)
        if [ "${2:-}" = runserver ] && ! wants_help "$@"; then
            if wants_demo "$@"; then
                cmd=$1
                shift 2
                for arg; do
                    shift
                    [ "$arg" = --import-csv-if-empty ] || set -- "$@" "$arg"
                done
                set -- "$cmd" demo "$@"
                echo "monitor-app: $PROJECT_DIR がマウントされていないため、倉庫在庫のデモを起動します(自分のプロジェクトは -v \"\$PWD\":/project でマウント)" >&2
            else
                prepare_project "$@"
            fi
        fi
        ;;
esac

# --- 3. 実行 ---------------------------------------------------------------------
exec "$@"
