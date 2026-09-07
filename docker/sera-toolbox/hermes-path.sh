# Login shells reset PATH via /etc/profile. Restore the image contract
# so login and non-login resolve the same binaries. Sourced, not exec'd.
# Keep in sync with the runtime Dockerfile ENV PATH.
umask 002
need_ok=1
for d in /opt/hermes/bin /opt/hermes/.venv/bin /command; do
  case ":${PATH:-}:" in
    *":$d:"*) ;;
    *) need_ok=0 ;;
  esac
done
if [ "$need_ok" != 1 ]; then
  PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/command:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
fi
export PATH
