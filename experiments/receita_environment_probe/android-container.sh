#!/usr/bin/env bash
# Wrap only this disposable worker's emulator; SDK/AVD preparation stays upstream.
set -euo pipefail
if [[ "${ANDROID_CONTAINER:-false}" != true ]]; then exit 0; fi
receita_sdk="${ANDROID_HOME:?Android SDK missing}"
test -c /dev/kvm
test -x "$receita_sdk/emulator/emulator"
test ! -e "$receita_sdk/emulator/emulator.receita-real"
mv "$receita_sdk/emulator/emulator" "$receita_sdk/emulator/emulator.receita-real"
cat > "$receita_sdk/emulator/emulator" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
receita_sdk="${ANDROID_HOME:?}"
exec docker run --rm --name receita-android-avd --device /dev/kvm --network host \
  --cpus 2 --memory 4g --shm-size 1g --cap-drop ALL --security-opt no-new-privileges \
  --env ANDROID_HOME --env ANDROID_SDK_ROOT \
  --env "ANDROID_AVD_HOME=$HOME/.android/avd" \
  --mount "type=bind,src=$receita_sdk,dst=$receita_sdk,readonly" \
  --mount "type=bind,src=$HOME/.android,dst=$HOME/.android" \
  --entrypoint "$receita_sdk/emulator/emulator.receita-real" receita-android-runtime:probe "$@"
WRAPPER
chmod +x "$receita_sdk/emulator/emulator"
