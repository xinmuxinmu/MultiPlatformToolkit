"""酷狗音乐 · 每日领 VIP 与乐迷团。

签名移植自 MakcRe/KuGouMusicApi（MIT）：
- util/helper.js 的 signatureAndroidParams / signatureWebParams /
  signatureRegisterParams
- util/util.js 的 calculateMid
- util/crypto.js 的 rsaEncrypt（无填充 RSA，乐迷团 token 用）

这四个函数都拿 Node 原样跑了一遍与 Python 对齐，不是照抄注释猜的。
"""
