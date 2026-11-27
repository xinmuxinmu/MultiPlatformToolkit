"""小黑盒（Heybox）每日任务。

签名算法移植自 heybox-daily 的 src/signature.ts（其注释标明改编自
XiaHouSheng/heybox-core，MIT）。网页版接口用这一套 web 签名就够，
不需要 App 端那套 SHA512 变体。

签到接口与任务列表都用 GET + web 签名——这点是拿匿名请求实测出来的：
不带登录态时返回 {"status":"login"} 而不是「签名错误」，说明签名算对了。
"""
