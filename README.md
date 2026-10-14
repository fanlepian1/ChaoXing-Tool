<p align="center">
  <a href="https://www.springing.top" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">Spring-Superstar</h2>
  <p align="center">
    学习通在线刷课脚本 超星 学习通 云端刷课
  </p>
</p>

本项目宗旨是帮助大学生们解放双手，根据教程操作后就可以完成刷课，简单易懂，仅需手机就可以使用

>如果本项目对希望各位同学们给本仓库点一个免费的Star或者给小春子点一个Follow 谢谢大家！

![截图](/images/star.png)

## 用前必读必读！
默认fork仓库之后是公开的状态，也就是说你的账号和密码是处于一个明文状态！！很危险！！建议把仓库权限改为私有

之前本仓库就因为明文账号密码泄露导致一些同学账号被盗拿来恶作剧(不得不说世界上还是坏人多)，故提前在此说明，希望大家周知

## 快速开始

<a href="https://blog.springing.top/p/20241119/" target="blank">点击我查看操作说明</a>

<a href="https://github.com/Samueli924/chaoxing" target="blank">灵感来源</a>

## 题库接入(GitHub Actions 版)

这个仓库在 Samueli924/chaoxing 的基础上补上了一整套**可自动积累的题库**，并且把配置全部走 secrets，
不需要把账号密码写进仓库。

### 1. 它解决了什么

* 原本命令行启动(`python main.py -u .. -p .. -l ..`)时题库配置为空，**题库根本不生效**，章节检测只能随机选答案；
* 现在 `main.py` 支持 `CHAOXING_TIKU_*` 环境变量与 `--tiku-*` 参数，Actions 里直接映射 secrets 即可；
* 新增自带题库 `TikuLocal`：答案落在仓库内的 `tiku/bank.json`，每轮运行攒到的答案会自动合并回题库并提交回仓库，
  下一轮直接命中，**不再重复消耗第三方题库额度**；
* 新增 `auto` 模式：按你配置过的凭据自动组回退链，离线题库永远排在最前面。

### 2. 手动触发（推荐先用这个）

`Actions` → 左侧 `刷课` → 右上 `Run workflow` → 选分支 `main` → 按需填下面几个框 → 绿色按钮。

| 输入框 | 作用 | 留空时 |
| --- | --- | --- |
| `course_list` | 只刷指定课程，逗号分隔 | 用 secrets 里的，再留空=全部课程 |
| `provider` | 临时换题库，例 `TikuLocal` 或 `TikuLocal,TikuGo,TikuYanxi` | 用 `TIKU_PROVIDER`，再留空=`auto` |
| `submit` | `true`=达到覆盖率就提交章节检测；`false`=只搜不交 | 用 `TIKU_SUBMIT`，再留空=`false` |
| `speed` / `jobs` | 倍速 / 并发章节数 | 用 secrets，再留空=1 / 4 |
| `bank_url` | 题库文件直链（json/txt），运行前自动导入 `tiku/bank.json` | 不导入 |
| `dry_run` | `true`=只检查配置与题库、不刷课 | `false` |

跑完之后在运行详情页最上方能看到 **“本次运行摘要”**（题库条数、是否启用、章节检测覆盖率、命中日志摘录），
日志本身也能在 Artifacts 里下载。

想每天定时自动跑：把 `.github/workflows/main.yml` 里 `#schedule:` 那两行的 `#` 去掉
（cron 用 UTC，`0 0 * * *` = 北京时间 08:00）。

### 3. 需要在仓库里配置的 Secrets

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | 说明 | 是否必需 |
| --- | --- | --- |
| `CHAOXING_USERNAME` | 学习通手机号 | 必需 |
| `CHAOXING_PASSWORD` | 学习通密码 | 必需 |
| `CHAOXING_COURSE_LIST` | 课程ID，英文逗号分隔；留空=全部课程 | 可选 |
| `CHAOXING_SPEED` / `CHAOXING_JOBS` | 倍速 / 并发章节数 | 可选 |
| `TIKU_PROVIDER` | 题库提供方，留空默认 `auto` | 可选 |
| `TIKU_SUBMIT` | `true`=达到覆盖率就提交章节检测；`false`=只保存答案 | 可选(默认 false) |
| `TIKU_COVER_RATE` | 最低题库覆盖率(默认 0.9) | 可选 |
| `TIKU_TOKENS` | 言溪题库 / LIKE知识库的 token，逗号分隔 | 有则更准 |
| `TIKU_URL` | 自建 [tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter) 或自建题库接口地址 | 可选 |
| `TIKU_LOCAL_API` | 你自己的题库 HTTP 接口 | 可选 |
| `TIKU_GO_AUTHORIZATION` | GO题解除限流的 Authorization | 可选 |
| `TIKU_SILICONFLOW_KEY` / `TIKU_SILICONFLOW_MODEL` | 硅基流动大模型答题 | 可选 |
| `TIKU_ENDPOINT` / `TIKU_KEY` / `TIKU_MODEL` | 任意 OpenAI 兼容接口 | 可选 |
| `NOTIFY_PROVIDER` / `NOTIFY_URL` / `TG_CHAT_ID` | 跑完推送通知 | 可选 |

### 3.1 题库到底怎么才能“用上”——按投入从少到多选一条

**第 0 档：什么都不配（可用，零成本）**
`provider` 默认 `auto`，会组出 `TikuLocal → TikuGo`：先在仓库内的 `tiku/bank.json` 里查，
查不到就问免费的 GO题（q.icodef.com）。这一档就能答一部分题，且每轮搜到的答案会自动写进
`tiku/bank.json` 并提交回仓库，**下一轮直接命中**。

**第 1 档：只开免费通道 + 攒题库（推荐先跑这个）**
1. 什么都不加，先手动触发一轮，`submit` 留空（只搜不交）；
2. 跑完看运行摘要里的 **章节检测题库覆盖率**；
3. 覆盖率稳定在 0.9 以上后，把 `submit` 改成 `true`，之后章节检测就会自动提交。

**第 2 档：加一个付费/专业题库（命中率最高，要 token）**
- 言溪：注册 https://tk.enncy.cn/ 拿 token → 加 secret `TIKU_TOKENS=你的token`（多个用逗号分隔）；
- 或自建 [tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter)：加 secret `TIKU_URL=http://你的地址/query`；
- 或大模型兜底：`TIKU_SILICONFLOW_KEY=sk-...`（或 `TIKU_ENDPOINT`+`TIKU_KEY`+`TIKU_MODEL`）。
  这些都不用改代码：`auto` 会按你填了哪些凭据自动把它们接进回退链。

**第 3 档：把现成题库灌进去（想自己掌控答案）**
```bash
# 本地跑(或在 Actions 里手动触发时把直链填进 bank_url)
python scripts/tiku_tool.py import --input 我的题库.json 别人的题库.txt
python scripts/tiku_tool.py stats                 # 看有多少题
python scripts/tiku_tool.py lookup -q "某道题"     # 自检能不能命中
python scripts/tiku_tool.py import --input 修好的题库.json --sync-cache   # 手改了答案后立刻生效
```
题库文件支持 `题目----答案` / `题目$$答案` / Tab / CSV / `.json` / `.jsonl`，格式自动识别。

> 一行总结：**第 0 档就能用；第 1 档跑两轮攒够覆盖率再开提交；第 2/3 档是提高命中率和掌控答案。**

### 4. 维护自己的题库

```bash
# 导入题库(支持 json / jsonl / csv / txt，格式自动识别；txt 支持 题目----答案 / 题目$$答案 / 题目<TAB>答案 / csv)
python scripts/tiku_tool.py import --input 我的题库.json 别人的题库.txt

# 看看现在有多少题、缓存里有多少答案
python scripts/tiku_tool.py stats

# 自检某道题能不能命中(题库不准时先跑这个)
python scripts/tiku_tool.py lookup -q "以下哪个是网络层协议？"

# 把答案缓存合并回题库(workflow 每轮结束会自动执行)
python scripts/tiku_tool.py export

# 手动改了题库里的答案？让改动立刻生效(脚本是先查缓存的, 否则旧缓存会继续被优先使用)
python scripts/tiku_tool.py import --input 修好的题库.json --sync-cache

# 旧版升级上来的题库: 先同步缓存再补溯源信息(让人工修正不会被自动回写覆盖)
python scripts/tiku_tool.py migrate --sync-cache-to tiku/cache.json

# 缓存是派生产物, 怀疑被污染时可以直接清空重新积累(题库文件不受影响)
python scripts/tiku_tool.py cache stats
python scripts/tiku_tool.py cache clear
```

### 5. 推荐的第一次运行方式

1. 保持 `TIKU_SUBMIT=false` 跑一轮（手动触发即可）：题库会把这一轮所有搜到的答案写进 `tiku/cache.json`，
   并合并进 `tiku/bank.json` 后提交回仓库；
2. 看运行摘要或日志里的「章节检测题库覆盖率」。覆盖率稳定在 0.9 以上后，把 `TIKU_SUBMIT` 改成 `true`，
   之后的章节检测就会用题库答案自动提交；
3. 需要解锁的章节必须提交检测，这种课程建议直接开 `TIKU_SUBMIT=true` 并配合题库。

### 6. 题库文件格式与匹配规则

题库文件格式(`tiku/bank.json`)：

```json
{
  "题干内容": "A",
  "带元信息的题干": { "answer": "B", "type": "single", "source": "我的题库" },
  "多选题干": "A\nB\nC"
}
```

题面匹配做了归一化(忽略题号、`【单选题】`标签、`（2.0分）`分值、空格、全半角、句读标点等)，
默认还有一层**包含式**模糊匹配：题库题面比卷面多／少几个字(比如多一句“请选择”)也能命中。

安全底线是刻意设计的：

* 只容忍“多字／少字”，**不容忍中间替换** —— “世界上最大的洋／洲”这类仅差一字的题目必须判为未命中；
* 数字与运算符会被完整保留：`3.5` 与 `35`、`1+1` 与 `11`、`(0,1)` 与 `[0,1]` 都是不同题目；
* 包含式匹配不允许切在字母/数字中间（`m 是什么单位` 不会命中 `cm 是什么单位`），
  多出来的片段含否定词（不／非／无…）时也判为不同题目（`不属于…` 不会偷 `属于…` 的答案）；
* 多个候选答案不同且分数接近时直接放弃；相似度兜底(`local_similarity`)默认关闭。

宁可“没搜到”（脚本会随机选一个并在日志里标注），也不会拿错答案去提交。

几个安全设计：

* 回写只补新题、并更新“之前由缓存自动写进去”的答案，**人工核对过/导入的答案不会被自动覆盖**；
* 题库文件解析失败、或你用的是非 `.json` 的文本题库时，**回写会直接拒绝并说明原因**，绝不覆盖你的原文件；
* 坏 JSON 不会被猜着解析（不会把 JSON 片段当答案）；答案为空的条目会提示而不是静默丢弃；
* 写盘是原子的（临时文件 + 替换），并发/中断不会留下半截题库。

### 7. 本地调试

```bash
cp config_template.ini config.ini        # 本地用文件配置更顺手
export CHAOXING_TIKU_PROVIDER=auto       # Windows 用 set / $env:
python main.py --config config.ini
```

临时覆盖某个题库参数：

```bash
python main.py -u 手机号 -p 密码 --tiku-provider TikuLocal,TikuGo --tiku-local-bank tiku/bank.json \
  --tiku-export-bank --tiku-opt local_threshold=0.9
```

第一次用 `python main.py --help` 可以看到全部 `--tiku-*` 参数。

## 赞助
>如果觉着代码对你有帮助，可以赞赏一下开发者

![截图](/images/reward.jpg)
