#!/system/bin/sh
# 拉随申办 + app 前台(防 Greezer Denial)
monkey -p mobi.w3studio.apps.android.shsmy.phone -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
am start -n com.hospitalhelper.controller/.MainActivity >/dev/null 2>&1
sleep 4
MAN=$(cat /data/local/tmp/st.json)
echo "MANIFEST_LEN=${#MAN}"
am broadcast -a com.hospitalhelper.controller.SELF_TEST \
  -n com.hospitalhelper.controller/.SelfTestReceiver \
  --es manifest "$MAN"
echo "BC_RC=$?"
