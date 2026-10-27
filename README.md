Print Workbench / a1-watchdog
A small tool that watches your 3D printer for you. Once installed, it has four pages:

History ledger: who printed, how long it took, and whether it succeeded or failed—all recorded

Real-time status: nozzle temperature, progress, and time remaining

Filament inventory: how many grams are left on each spool, with advance warnings when it’s running low

Spaghetti failure monitoring: capture frames, score them, and alert. If the current layer looks acceptable to the human eye, click “This level is acceptable”; it will only alert again once the severity increases.


做了个工作台 四个页面：· 历史台账：谁打的、打了多久、成功还是失败，全记账 · 实时状态：喷头温度、进度、剩余时间 · 耗材库存：剩多少克，快没了提前提醒 
· 炒面监控：抓帧、判分、报警，人眼看着能接受可以点一次（这个程度可以接）」，程度再上升才重新提醒
开源版本用的开源模型 原理是给画面打分 分数高发出警告
但是限于限制 只有局域网模式才能有暂停权限
但是局域网开了用不了拓竹APP 可以自己取舍一下
全厂家打印机应该都能用
不想用局域网可以自己接一个微信提示 炒面报警就行


## 支持哪些机器

拓竹 A1、A1 mini、P1 系列是实测跑通的（局域网 MQTT 加摄像头）。其他厂家的机器，只要肯放开 MQTT 和一路摄像头，原则上能接，但没实测，配置文件按拓竹那套写。

只支持 Windows。

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

不想为暂停去折腾局域网模式的，就把它当个报警器用，这件事本身就已经解决了「半夜炒面没人管」。

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

默认阈值 pause 0.40、warn 0.30，连续二十次过 pause 才真暂停，大约四十秒。

画面糊了不判。失焦、过曝、镜头起雾的时候，分类模型会把整片平滑画面读成满屏面条，分数直接冲到 0.9 以上，这是误报最主要的来源。所以每帧先算中心区域的锐度（拉普拉斯方差），低于 detection.min_sharpness（默认 40）就跳过这一帧：不报警，也不打断已有的连续计数，日志一分钟记一条。实测 560 张真机帧，误报帧锐度中位数 20，正常帧 63；加上这道门，那批误报一条都没触发。想更松就把 min_sharpness 调低，填 0 等于关掉这道门。

嫌报警太慢就把 detection.pause_after_consecutive 调回 2 到 5。要改判定松紧就动 detection.local_pause_threshold 和 detection.local_warn_threshold。换了模型记得重新标阈值，别人的模型阈值不能直接抄。

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

## 许可

源码公开，可以免费用、随便改、给朋友装，也可以在这个基础上做自己的东西。

不能商用。不许卖，不许打包进收钱的产品或服务里，也不许拿它做收费的技术支持、代部署、代运营。公司内部自己用不算商用，拿去给别人做付费服务算。想商用单独找我谈。

完整条款在 LICENSE 里，是 PolyForm Noncommercial 1.0.0，以英文原文为准。

真觉得省了你的事、手上又刚好宽裕，可以请我喝杯咖啡：https://afdian.com/a/KONATAZ

不方便也完全没关系。这里没有付费版，也没有打算做的付费功能。

有问题提 issue。
