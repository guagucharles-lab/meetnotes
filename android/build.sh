#!/bin/sh
# Builds Meetnotes.apk with the Debian/Ubuntu Android tools:
#   apt-get install openjdk-17-jdk-headless android-sdk-platform-23 aapt dalvik-exchange zipalign apksigner
# The signing key (meetnotes.keystore) must stay the same, or Android refuses
# to install an update over the existing app.
set -eu
cd "$(dirname "$0")"
SDK_JAR=/usr/lib/android-sdk/platforms/android-23/android.jar
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes"

# 1) resources + manifest -> R.java and a resource-only APK (resources.arsc stored, not compressed)
aapt package -f -m -0 arsc -M AndroidManifest.xml -S res -I "$SDK_JAR" -J "$OUT/gen" -F "$OUT/unsigned.apk"
# 2) Java 8 bytecode against the Android API
javac -source 8 -target 8 -bootclasspath "$SDK_JAR" -encoding UTF-8 -nowarn -Xlint:-options \
      -d "$OUT/classes" $(find src "$OUT/gen" -name '*.java')
# 3) -> Dalvik bytecode
dalvik-exchange --dex --output="$OUT/classes.dex" "$OUT/classes"
(cd "$OUT" && aapt add -f unsigned.apk classes.dex >/dev/null)
# 4) align and sign (v1 + v2 + v3 signatures)
zipalign -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
if [ ! -f meetnotes.keystore ]; then
  keytool -genkeypair -keystore meetnotes.keystore -alias meetnotes -keyalg RSA -keysize 2048 -validity 10000 \
          -storepass meetnotes -keypass meetnotes -dname "CN=Meetnotes" >/dev/null 2>&1
fi
apksigner sign --ks meetnotes.keystore --ks-pass pass:meetnotes --key-pass pass:meetnotes \
               --out "$OUT/Meetnotes.apk" "$OUT/aligned.apk"
apksigner verify "$OUT/Meetnotes.apk"
echo "Built $OUT/Meetnotes.apk"
