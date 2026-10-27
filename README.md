# a1-watchdog

看拓竹打印机的摄像头画面，认出炒面（打印件糊成一团）就在电脑上弹窗、给手机发消息。

只支持 Windows。打印机是拓竹 Bambu Lab 的 A1、A1 mini、P1 系列，走局域网 MQTT 加摄像头，需要打印机开着局域网模式并拿到访问码。

## 能做什么

取摄像头画面，判断有没有炒面，弹窗、发推送、把出问题的帧存下来。

默认不向打印机发任何控制命令。拓竹从固件 01.08 开始给控制类指令加了授权校验（RSA 加 X.509 证书签名），没有官方签名的暂停、启动命令会被拒绝，打印机回 HMS 0500_0500_0001_0007，err_code 0x05024007。所以默认配置下这个程序只看不控，报警了自己到打印机上按暂停。

想让它自动暂停，得先确认固件没有这道校验（比如 01.07.x），再把 alert.allow_pause 设成 true。风险自负，见下面「自动暂停」。

## 装起来

需要 Python 3.10 或更新。装的时候记得勾上「Add python.exe to PATH」。

```bat
pip install -r requirements.txt
```

省事的做法是双击 run.bat。它会检查 Python 在不在，把两个模板复制成正式配置，然后启动。

想手动也行：

```bat
copy a1-watchdog.config.example.json a1-watchdog.config.json
copy a1-watchdog.secrets.example.json a1-watchdog.secrets.json
```

a1-watchdog.config.json 放打印机 IP、序列号、检测参数、推送通道，这个文件可以发给别人看。
a1-watchdog.secrets.json 放访问码、API key、推送密钥，这个不要发人，也不要提交到仓库。

两个模板里的值都以 PUT_ 开头，把 PUT_ 开头的换成自己的。忘了改也不要紧，自检会指名道姓告诉你哪一个没填。

改完先自检，五项都通再启动：

```bat
python a1_watchdog.py --check
```

模型不在仓库里，所以全新克隆时 2/5 会红，这是正常的。要么自己准备模型（见下面「模型从哪来」），要么把 detection.engine 改成 gadget 走云端。

启动：

```bat
python a1_watchdog.py
```

跑起来之后浏览器打开 http://127.0.0.1:8099/ 可以看画面，给 OBS 当浏览器源也行。

## 微信收报警

不需要服务器、域名、公网 IP。报警时程序直接 POST 一条消息给第三方转发服务，由它的公众号送到微信。本机断网发不出去，其他时候不用管。

四个通道挑一个，填 type 和 key：

| type | 服务 | 免费额度 | 拿 key | 消息落在哪 |
|---|---|---|---|---|
| pushplus | PushPlus | 200 条/天 | 微信扫码登录，首页复制 token | 公众号「pushplus 推送加」 |
| serverchan | Server酱 | 5 条/天 | 微信扫码登录，复制 SendKey | 服务号「方糖」 |
| wecom | 企业微信群机器人 | 无限 | 建群，群设置，群机器人，复制 Webhook | 企业微信 App |
| wxpusher | WxPusher | 无限 | 建应用拿 appToken，关注公众号拿 UID | 公众号 |

炒面一天报警一两次，Server酱 的额度够用，扫码也最快。想长期用建议 PushPlus。

### 怎么配

先按上表扫一次码，把 key 记下来。

然后一条命令试一下，不用先改配置，token 直接写在命令里：

```bat
python a1_watchdog.py --push-test pushplus:你的token
```

微信里立刻能收到一条测试消息，命令行打「成功」就是通了。企业微信的 key 是整条 URL，写成 --push-test wecom:https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx。

通了以后填进配置让它真发。a1-watchdog.config.json 里：

```json
"push": { "enabled": true, "type": "pushplus", "key": "" }
```

token 写进 a1-watchdog.secrets.json 的 push_key：

```json
{ "push_key": "你的token" }
```

key 放 secrets 是因为 config 经常要贴出来问人，token 写在那里等于公开。secrets 里的 push_key 优先级比 config 里的 key 高。

### 推送长什么样

```
A1 炒面警报

打印机：A1（192.168.1.50）
详情：质量=bad 分数=82 帧=20261025-1830-FAILED.jpg
时间：2026-10-25 18:30:11
状态：自动暂停未启用，请立刻手动暂停，帧文件 frames\20261025-1830-FAILED.jpg
```

打印机报错（HMS 码）变化时只写日志，不推送也不弹窗，避免把长任务里常见的报错当成故障。同一类报警默认 60 秒内只推一条，靠 push.min_interval_sec 控制。

### 不发图，只给路径

报警帧存在本机 frames 目录，推送里给的是文件路径。故意不传图床：画面里常有房间、桌面、人，默认往外传不合适。想要图可以自己接一段上传，各通道的 markdown 都支持公网图片 URL。

## 自动暂停

alert.allow_pause 默认 false，改成 true 才会在报警时发暂停命令：

```json
"alert": { "popup": true, "printer_error": true, "allow_pause": true }
```

先看固件版本。01.08 及以上有指令授权校验，暂停会被拒，日志里是「自动暂停失败」，推送里也会这么写，还是得自己按。

## 命令行

```bat
python a1_watchdog.py                :: 正常跑
python a1_watchdog.py --push-test    :: 只测推送通道，不连打印机
python a1_watchdog.py --check        :: 自检摄像头、MQTT、检测引擎
python a1_watchdog.py --once         :: 取一帧判定一次就退出
python a1_watchdog.py -v             :: 详细日志
```

## 检测引擎

detection.engine 三个值。

local：本地模型，一次 200 到 400 毫秒，1 到 2 秒一检。模型文件自己准备，放进 models 目录（本仓库不带权重）。依赖 numpy、pillow，再加 ai-edge-litert 或 onnxruntime 其中一个，看模型是什么格式。

两个格式都支持，按文件后缀自动挑检测器：

| 文件 | 类型 | 输出 | 速度 |
|---|---|---|---|
| .tflite | 4 类检测模型 | Spaghetti / Blob / Warping / Crack 四类，带检测框 | 200 到 400 毫秒 |
| .onnx | 2 类分类模型 | 只有一个炒面概率 | 30 到 60 毫秒 |

找文件的顺序：环境变量 A1_WATCHDOG_MODEL，然后 models/spaghetti_yolov8n_4cls.tflite、models/spagcls.onnx，最后 models 目录里任意一个 .tflite 或 .onnx。也可以直接在配置里写 detection.model_path 指定绝对路径。

默认阈值 pause 0.40、warn 0.30，连续两帧过 pause 才真暂停。改 detection.local_pause_threshold 和 detection.local_warn_threshold。换了模型记得重新标阈值，别人的模型阈值不能直接抄。

gadget：OctoEverywhere Gadget 云端识别，要 API key，填在 secrets 的 gadget_api_key。单帧 2.5 到 5 秒，而且要连续 3 到 5 帧才给暂停建议，从炒面出现到它敢说该停大概 17 到 22 秒。稳，但慢。

both：两个都跑，任一报警就报。最吵，不建议。

## 没放模型也能先跑起来

仓库里不带模型权重，所以刚克隆下来时第二项自检会红。这时候直接启动也不会崩：
看门狗照常连打印机、拿画面、存帧、报进度，只是不做炒面判定，也不会自动暂停。
日志里会写明「本地检测模型不在：<路径>」，把模型放到那个位置后重启就开始判定。
同理，装依赖不全或者模型文件打不开时也只是降级成不判定，不会整个进程退出。

## 模型从哪来

仓库不带模型权重。一是体积（onnx 九兆、tflite 十几兆，进仓库每次 clone 都要拉），二是权重都是别人训练或转换的，授权未必允许跟着代码一起分发。

三条路。

一是自己训。用 ultralytics 的 YOLOv8n-cls 在炒面数据集上训一个 2 分类（clean / spaghetti），导成 ONNX。训练集搜「3d printing spaghetti detection dataset」，Hugging Face 和 Kaggle 上都有现成的。1080p 上一帧大概 30 毫秒，够用。

二是让别人训好的权重转格式。拿到 .pt 之后：

```bat
yolo export model=你的模型.pt format=onnx imgsz=224
```

出来的文件丢进 models 目录，改名叫 spagcls.onnx，或者直接用 detection.model_path 指过去。

三是不训。detection.engine 改成 gadget，用 OctoEverywhere 的云端模型，只需要一个 API key，本地什么都不用装。慢一点，稳一点。

放好模型跑一次自检，四项都打勾才算通：

```bat
python a1_watchdog.py --check
```

## 常见问题

画面连不上或者一会儿就断：拓竹固件同一时刻只允许一个 MQTT 连接和一路摄像头。Bambu Studio、Home Assistant、OctoEverywhere 插件都会抢，抢输了就断线。只留一个客户端，或者用 OctoEverywhere 的 MQTT Relay 合并连接。

打印机名字显示的是配置文件里那个：固件本地 MQTT 不带打印机昵称，只有拓竹云接口有。想显示真名得配 mqtt.cloud_secrets_path，用账号令牌查云接口。

同一台机器不能同时跑两个看门狗：程序用锁文件防重复启动，第二个会直接退出并告诉你 PID。

杀毒软件报可疑：程序会调 powershell msg 弹窗、会连摄像头和 MQTT。不放心可以先 --check 只跑自检，或者把 alert.popup 关掉只留推送。

## 支持作者

代码 MIT，随便用随便改，功能全在这里，没有要买的东西。

用着顺手、想请我喝杯咖啡的，去爱发电：https://afdian.com/a/KONATAZ

发电不换功能，也不会有付费版专属功能。发了电的，issue 我先看。

## 许可

MIT。

有问题提 issue。
