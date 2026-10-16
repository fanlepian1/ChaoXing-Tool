<p align="center">
  <a href="https://github.com/fanlepian1/SuperStar" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">SuperStar</h2>
  <p align="center">
    学习通 / 超星 自动刷课 · 云端运行 · 内置可积累题库 · 支持扫码登录
  </p>
</p>

用 GitHub Actions 云端刷课：不用开电脑、不用装环境，手机点一下就能跑。
内置题库会**自己积累**（每轮搜到的答案自动写回仓库，下一轮直接命中），章节检测支持指定覆盖率自动提交。

---

## 快速上手（5 分钟）

### 第 1 步：配置 Secrets

打开 `Settings → Secrets and variables → Actions → New repository secret`，最少加这两个：

| Secret | 值 |
| --- | --- |
| `CHAOXING_USERNAME` | 你的学习通手机号 |
| `CHAOXING_PASSWORD` | 你的学习通密码 |

> 建议顺手把仓库改成私有（`Settings → General → Danger Zone → Change visibility → Private`），
> 因为运行日志和 artifact 里会包含你的课程信息。

### 第 2 步：手动跑第一轮

`Actions` → 左侧选 **刷课** → 右上 **Run workflow** → 分支选 `main` → **直接点绿色按钮**（所有输入都可以留空）。

> 只有手动触发才会运行：推送代码、题库回写提交都不会自动跑。想改成定时/推送自动跑，
> 见文末「让它自己跑起来」。

### 第 3 步：看运行摘要

跑完在运行详情页**最上方**看「本次运行摘要」：

| 看到的指标 | 含义 |
| --- | --- |
| 题库条数 | 仓库里已积累了多少题（第一次跑完就会从 0 变成几十上百条） |
| 章节检测题库覆盖率 | 这次章节检测有多少比例是靠题库答出来的 |
| 命中与覆盖率（日志摘录） | 每题是怎么答出来的（本地题库 / 缓存 / 第三方题库） |

**第一次请把 `submit` 留空**（= 只搜答案、不提交）。等摘要里的覆盖率稳定在 **0.9** 以上，再在下一次运行时把 `submit` 填 `true`（或把 secret `TIKU_SUBMIT` 设为 `true`），章节检测就会自动提交。

跑完仓库里会自动多出两个文件：`tiku/bank.json`（题库）和 `tiku/cache.json`（本轮答案缓存）——这是机器人提交回来的，正常现象。

### 就这三步，完了。

下面按需继续看：只想刷指定课程 / 不想用密码 / 想让题库更准 / 遇到问题。

---

## 常见需求

### 只刷指定课程

手动触发时把 `course_list` 填上课程ID（英文逗号分隔），或写进 secret `CHAOXING_COURSE_LIST`。

**怎么拿课程ID**：手动触发时把 **`dry_run` 填 `true`**，跑完日志里会打印你的课程表：

```
当前账号下的课程列表(把 courseId 填进 CHAOXING_COURSE_LIST 即可):
  1. 中国共产党历史
      courseId = ***  (防打码写法: 268 053 034 -> 实际填 268053034)
      classId = None
  2. 中华优秀传统文化
      courseId = 267399796
```

* 课程ID 是 **9 位纯数字**；
* 带空格的那种写法只是为了绕过 GitHub 的 secret 打码，**填的时候把空格去掉**；
* 填错也不怕：脚本会容忍中文逗号、换行、空格、引号、全角数字、`id:` 前缀，并且**同时匹配 `courseId` 与 `classId`**；
* 实在不想填就**留空** —— 会刷账号下的**全部课程**。

### 不想把密码存在仓库里（扫码 / cookie 登录）

密码登录偶尔会遇到风控（连续输错会冻结约 15 分钟）。两种替代方式：

**方式 A：本地扫码一次，存成 cookie（推荐，之后都不用扫）**

在电脑上运行：

```bash
python scripts/qr_login.py --browser        # 弹出浏览器窗口, 在里面扫码/输密码并确认
python scripts/qr_login.py --export-cookie  # 打印一行 _uid=...;_d=...;fid=...
python scripts/qr_login.py --check-cookie   # (可选) 验证这串 cookie 还有效没效
```

把打印的那一行存进 secret **`CHAOXING_COOKIE`**，然后可以把 `CHAOXING_PASSWORD` 删掉。
之后登录顺序是：**cookie 优先 → 失效自动回退密码**（密码还在的话）。

> 快捷方式：仓库外还提供一个零依赖的小工具（双击即用，自动复制 cookie 到剪贴板）：
> `扫码取cookie.py` + `运行扫码取cookie.bat`。

**方式 B：直接在 Actions 里扫码（每次运行扫一次）**

手动触发时把 `login` 填 `qr`：运行页面顶部会显示二维码图片，用**学习通 App → 扫一扫**扫它，
并在手机上点【确认登录】，脚本拿到会话后自动带 `--use-cookies` 继续刷课。

### 让题库更准（可选，按投入从少到多）

| 档位 | 做什么 | 效果 |
| --- | --- | --- |
| 0 | 什么都不配 | `auto` 组链：**本地题库 → GO题**（免费），题库每轮自动增长 |
| 1 | 先跑两轮，覆盖率稳定后再开 `submit` | 不花钱，先把免费渠道榨干 |
| 2 | 加 token：`TIKU_TOKENS=言溪token`（[tk.enncy.cn](https://tk.enncy.cn/)）、自建 [tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter) 填 `TIKU_URL`、大模型兜底 `TIKU_SILICONFLOW_KEY` 或 `TIKU_ENDPOINT`+`TIKU_KEY`+`TIKU_MODEL` | 命中率明显提高，不用改代码 |
| 3 | 灌自己的题库文件：`python scripts/tiku_tool.py import --input 我的题库.json`（也支持手动触发时把直链填进 `bank_url`） | 答案完全可控 |

题库文件支持 `题目----答案`、`题目$$答案`、Tab、CSV、`.json`、`.jsonl`，**格式自动识别**。

### 手动维护题库

```bash
python scripts/tiku_tool.py stats                    # 有多少题 / 缓存多少条
python scripts/tiku_tool.py lookup -q "某道题"        # 自检能否命中
python scripts/tiku_tool.py import --input 新题库.json    # 导入
python scripts/tiku_tool.py export                   # 把缓存合并回题库（workflow 每轮自动做）
python scripts/tiku_tool.py import --input 修好的.json --sync-cache   # 手改答案后立刻生效
python scripts/tiku_tool.py cache clear              # 缓存被污染时清空重来（题库文件不受影响）
```

### 让它自己跑起来（默认只手动）

编辑 `.github/workflows/main.yml`，把对应段的 `#` 去掉即可：

```yaml
  # push:
  #   branches: [ main ]

  #schedule:
  #  - cron: "0 0 * * *"      # UTC, 等于北京时间每天 08:00
```

也可以同时跑多个：刷课本体不排队，可以连点几次 `Run workflow`（例如不同 `course_list`）；
只有「回写题库」那一步会排队，避免互相覆盖。

---

## 手动触发参数一览

`Actions → 刷课 → Run workflow`

| 输入框 | 作用 | 留空时 |
| --- | --- | --- |
| `course_list` | 只刷指定课程ID，英文逗号分隔 | 用 secret，再留空=全部课程 |
| `provider` | 临时换题库，例 `TikuLocal` / `TikuLocal,TikuGo` | 用 `TIKU_PROVIDER`，再留空=`auto` |
| `submit` | `true`=达到覆盖率就提交章节检测 | 用 `TIKU_SUBMIT`，再留空=`false` |
| `speed` / `jobs` | 视频倍速 / 并发章节数 | 用 secrets，再留空=1 / 4 |
| `login` | 登录方式：`auto`/`qr`/`cookie`/`password` | `auto`（有 cookie 就用 cookie） |
| `qr_timeout` | 扫码等待秒数（`login=qr` 时） | 300 |
| `qr_upload` | 是否把二维码传成图片链接便于手机扫 | `true` |
| `bank_url` | 题库文件直链，运行前导入 | 不导入 |
| `dry_run` | `true`=只登录并打印课程表，不刷课 | `false` |

---

## 常见问题

**Q：报 `用户名或密码错误`，但我确定密码是对的？**
按顺序排查（脚本也会把下面这些信息打进日志）：
1. **账号被临时冻结**：短时间内多次密码错误会冻结约 15 分钟，期间密码正确也报错 → 等一会儿再试；
2. **secret 里粘了尾换行/空格/引号**：日志里的「账号信息: 用户名长度=… 密码长度=…」会提示"首尾有空白/换行(已自动去掉)"，新版会自动清理；
3. **密码含全角字符**（输入法带进来的）：日志会标"含全角字符"→ 去 secret 里切英文输入法重打一遍；
4. 还是不行就用 **cookie 登录**（见上），把 `CHAOXING_PASSWORD` 删掉。

**Q：报 `未找到指定课程ID`？**
日志会把你的课程表打出来对照。注意课程ID是 **9 位纯数字**；脏字符（引号/换行/全角/中文逗号）新版都会自动处理，也支持填 `classId`。不想折腾就留空刷全部课程。

**Q：章节检测没提交？**
`submit` 没开，或覆盖率没到 `cover_rate`（默认 0.9）。脚本**宁可漏答也不用错答案**，覆盖率不够就不交。

**Q：题库会不会被自动改错？**
- 只补新题、更新"之前由缓存自动写入"的答案，**人工导入/核对过的答案不会被覆盖**；
- 坏 JSON 不会被猜着解析；非 `.json` 的文本题库会拒绝回写并说明原因；
- 写盘是原子的（临时文件 + 替换），并发或中断不会留下半截文件；
- 匹配只容忍"多字/少字"，**不容忍中间替换**，数字与运算符完整保留（`3.5` ≠ `35`、`(0,1)` ≠ `[0,1]`）。

**Q：为什么我的仓库里一直多出 `chore(tiku)` 提交？**
那是刷课结束后把这一轮攒到的答案合并回题库 —— 题库就是这样一轮轮长起来的。它是派生产物，想清空直接改 `tiku/bank.json` 即可。

---

## 本地运行（可选）

```bash
pip install -r requirements.txt

# 用 config.ini（推荐，模板见 config_template.ini）
cp config_template.ini config.ini
python main.py --config config.ini

# 或者全用命令行
python main.py -u 手机号 -p 密码 -l 268053034
python main.py --use-cookies                 # 用 cookies.txt 里的会话
python main.py --list-courses                # 只登录并打印课程表
python main.py --help                        # 全部 --tiku-* 参数
```

配置优先级：`默认值 < config.ini < 环境变量 < 命令行参数`。

## 自检与测试

```bash
python -m unittest discover -s tests -t .     # 260+ 项单元测试
```

CI 里的「自检」步骤跑的就是它；它只做提示，失败了也不会挡住刷课。

## 目录结构

```
main.py                 入口(配置合并 / 登录 / 刷课 / 题库回写)
api/base.py             超星接口(登录、课程、任务点、章节检测提交)
api/answer.py           题库调度(tiku provider / 缓存 / 回退链)
api/tiku_bank.py        本地题库(归一化匹配、导入导出、原子写盘)
scripts/gen_config.py   secrets → config.ini
scripts/tiku_tool.py    题库维护命令行
scripts/qr_login.py     扫码登录 / 导出 cookie
tiku/bank.json          题库(随仓库提交, 越跑越全)
tiku/cache.json         本轮答案缓存(派生产物)
.github/workflows/      刷课工作流(SpeedRun 并行 + WriteBack 串行回写)
```

---

## 赞助

>如果觉着代码对你有帮助，可以扫码赞赏一下

![赞赏码](/images/reward.png)
