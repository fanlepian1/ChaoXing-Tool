# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import os
import sys
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import PriorityQueue
from threading import RLock
from typing import Any

from tqdm import tqdm

from api.answer import Tiku, CacheDAO
from api.base import Chaoxing, Account, StudyResult
from api.exceptions import LoginError, InputFormatError
from api.logger import configure_console_logger, logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor
from api.tiku_bank import (
    DEFAULT_BANK_PATH,
    DEFAULT_CACHE_PATH,
    DEFAULT_TIKU_CONFIG,
    LocalBank,
    collect_tiku_config_from_env,
    split_paths,
)


class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def stdin_is_interactive() -> bool:
    """当前是否处于可交互终端。

    除了常见的 isatty()==False, 还要防 sys.stdin 被彻底关闭(None)的情况 ——
    Windows 上通过某些调度器/服务方式启动时 stdin 可能是 None, 直接调用会 AttributeError。
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # 某些环境 isatty() 本身也会抛异常
        return False


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=None, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=None, help="同时进行的章节数 (默认4, 如果一个章节有多个任务点，不会限制同时处理任务点的数量)"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default=None, 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    parser.add_argument("--auto-sign", action="store_true", help="自动签到")

    # ---------------- 题库配置(命令行优先级最高, 方便 GitHub Actions 直接传参) ----------------
    tiku_group = parser.add_argument_group("题库")
    tiku_group.add_argument("--tiku-provider", type=str, default=None,
                            help="题库提供方, 英文逗号分隔可组回退链, 例: TikuLocal,TikuGo,TikuYanxi; "
                                 "auto=按已配置的凭据自动组链")
    tiku_group.add_argument("--no-tiku", action="store_true", help="强制关闭题库")
    tiku_group.add_argument("--tiku-submit", type=str_to_bool, default=None, nargs="?", const=True,
                            help="是否提交章节检测答案(true/false)")
    tiku_group.add_argument("--tiku-cover-rate", type=float, default=None, help="最低题库覆盖率")
    tiku_group.add_argument("--tiku-delay", type=float, default=None, help="每题查询间隔(秒)")
    tiku_group.add_argument("--tiku-tokens", type=str, default=None, help="言溪/LIKE知识库的 token, 逗号分隔")
    tiku_group.add_argument("--tiku-url", type=str, default=None, help="TikuAdapter(自建)接口地址")
    tiku_group.add_argument("--tiku-endpoint", type=str, default=None, help="AI 题库的 OpenAI 兼容 Endpoint")
    tiku_group.add_argument("--tiku-key", type=str, default=None, help="AI 题库的 API Key")
    tiku_group.add_argument("--tiku-model", type=str, default=None, help="AI 题库的模型名")
    tiku_group.add_argument("--tiku-go-authorization", type=str, default=None, help="GO题解除限流的 Authorization")
    tiku_group.add_argument("--tiku-siliconflow-key", type=str, default=None, help="硅基流动 API Key")
    tiku_group.add_argument("--tiku-local-bank", type=str, default=None,
                            help=f"本地题库文件, 逗号分隔(默认 {DEFAULT_BANK_PATH})")
    tiku_group.add_argument("--tiku-local-api", type=str, default=None, help="自建题库接口地址(TikuLocal 用)")
    tiku_group.add_argument("--tiku-cache-file", type=str, default=None,
                            help=f"答案缓存文件(默认 {DEFAULT_CACHE_PATH})")
    tiku_group.add_argument("--tiku-export-bank", action="store_true",
                            help="运行结束后把本次积累的答案缓存合并回本地题库文件")
    tiku_group.add_argument("--tiku-opt", action="append", default=None, metavar="KEY=VALUE",
                            help="任意 [tiku] 配置项, 可重复, 例: --tiku-opt local_threshold=0.9")

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()
        if "verbose" in common_config:
            common_config["verbose"] = str_to_bool(common_config["verbose"])

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))
    
    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed if args.speed else 1.0,
        "jobs": args.jobs,
        "notopen_action": args.notopen_action if args.notopen_action else "retry",
        "verbose": args.verbose,
    }
    return common_config, collect_tiku_config_from_env(), collect_notification_config_from_env()


# ============================================================================
# 配置合并: 默认值 < config.ini < 环境变量 < 命令行参数
# 这样 GitHub Actions 只需要把 secrets 映射成环境变量, 不必把密码写进仓库
# ============================================================================
COMMON_ENV_MAP = {
    "CHAOXING_USERNAME": "username",
    "CHAOXING_PASSWORD": "password",
    "CHAOXING_COURSE_LIST": "course_list",
    "CHAOXING_SPEED": "speed",
    "CHAOXING_JOBS": "jobs",
    "CHAOXING_NOTOPEN_ACTION": "notopen_action",
    "CHAOXING_USE_COOKIES": "use_cookies",
    "CHAOXING_VERBOSE": "verbose",
}

NOTIFICATION_ENV_MAP = {
    "CHAOXING_NOTIFY_PROVIDER": "provider",
    "CHAOXING_NOTIFY_URL": "url",
    "CHAOXING_NOTIFY_TG_CHAT_ID": "tg_chat_id",
}

TIKU_NUMERIC_KEYS = ("cover_rate", "delay", "local_threshold", "local_api_timeout")


def collect_notification_config_from_env(env: dict = None) -> dict:
    """收集推送通知相关的环境变量。"""
    env = os.environ if env is None else env
    conf: dict[str, str] = {}
    for env_key, conf_key in NOTIFICATION_ENV_MAP.items():
        raw = env.get(env_key)
        if raw is not None and str(raw).strip():
            conf[conf_key] = str(raw).strip()
    return conf


def build_tiku_config_from_args(args) -> dict:
    """把命令行里的题库参数转成 [tiku] 配置键(只收集显式传入的)。"""
    conf: dict[str, Any] = {}
    mapping = [
        ("tiku_provider", "provider"),
        ("tiku_tokens", "tokens"),
        ("tiku_url", "url"),
        ("tiku_endpoint", "endpoint"),
        ("tiku_key", "key"),
        ("tiku_model", "model"),
        ("tiku_go_authorization", "go_authorization"),
        ("tiku_siliconflow_key", "siliconflow_key"),
        ("tiku_local_bank", "local_bank"),
        ("tiku_local_api", "local_api"),
        ("tiku_cache_file", "cache_file"),
    ]
    for arg_name, conf_key in mapping:
        value = getattr(args, arg_name, None)
        if value is not None and str(value).strip():
            conf[conf_key] = str(value).strip()
    if getattr(args, "tiku_submit", None) is not None:
        conf["submit"] = "true" if args.tiku_submit else "false"
    if getattr(args, "tiku_cover_rate", None) is not None:
        conf["cover_rate"] = args.tiku_cover_rate
    if getattr(args, "tiku_delay", None) is not None:
        conf["delay"] = args.tiku_delay
    if getattr(args, "tiku_export_bank", False):
        conf["export_bank"] = "true"
    for item in (getattr(args, "tiku_opt", None) or []):
        if "=" not in item:
            logger.warning(f"忽略无效的 --tiku-opt: {item} (应为 KEY=VALUE)")
            continue
        key, _, value = item.partition("=")
        key = key.strip().lower()
        if key:
            conf[key] = value.strip()
    return conf


def resolve_auto_provider(tiku_config: dict) -> str:
    """provider=auto 时, 按已配置的凭据自动组一条回退链(离线优先)。"""
    chain = ["TikuLocal"]
    if tiku_config.get("go_authorization") or str_to_bool(tiku_config.get("go_enabled", "true")):
        chain.append("TikuGo")
    if tiku_config.get("tokens"):
        chain.append("TikuYanxi")
    if tiku_config.get("url"):
        chain.append("TikuAdapter")
    if tiku_config.get("siliconflow_key"):
        chain.append("SiliconFlow")
    if tiku_config.get("endpoint") and tiku_config.get("key"):
        chain.append("AI")
    return ",".join(chain)


def resolve_tiku_config(file_config: dict = None, args=None, env: dict = None) -> dict:
    """合并题库配置并做类型归整。"""
    conf: dict[str, Any] = dict(DEFAULT_TIKU_CONFIG)
    for key, value in (file_config or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            # 配置文件里的空值(模板里大量存在)不应该覆盖掉默认值
            continue
        conf[key] = value.strip() if isinstance(value, str) else value
    conf.update(collect_tiku_config_from_env(env))
    if args is not None:
        conf.update(build_tiku_config_from_args(args))

    for key in TIKU_NUMERIC_KEYS:
        if key in conf:
            try:
                conf[key] = float(conf[key])
            except (TypeError, ValueError):
                # 保留合理默认值, 而不是把字符串塞给 time.sleep / 比较运算
                if key in DEFAULT_TIKU_CONFIG:
                    conf[key] = float(DEFAULT_TIKU_CONFIG[key])
                    logger.warning(f"题库配置 {key} 不是数字, 已回退默认值 {conf[key]}")
                else:
                    conf.pop(key)
                    logger.warning(f"题库配置 {key} 不是数字, 已忽略")

    provider = str(conf.get("provider") or "").strip()
    if provider.lower() == "auto":
        provider = resolve_auto_provider(conf)
    if args is not None and getattr(args, "no_tiku", False):
        provider = ""
    conf["provider"] = provider
    return conf


def describe_tiku_config(tiku_config: dict) -> str:
    """生成一份脱敏后的题库配置摘要, 便于在日志/Actions 里核对。"""
    secret_keys = {"tokens", "key", "siliconflow_key", "go_authorization", "password"}

    def mask(value):
        text = str(value)
        if len(text) <= 4:
            return "****"
        return f"{text[:2]}****{text[-2:]}"

    parts = []
    for key in sorted(tiku_config):
        value = tiku_config[key]
        if value is None or str(value).strip() == "":
            continue
        parts.append(f"{key}={'****' if key in secret_keys else value}")
    return ", ".join(parts)


def build_common_config(args, file_config: dict = None) -> dict:
    """合并 common 配置: 文件 -> 环境变量 -> 命令行。"""
    conf: dict[str, Any] = dict(file_config or {})

    for env_key, conf_key in COMMON_ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        if conf_key in ("speed",):
            try:
                conf[conf_key] = float(value)
            except ValueError:
                logger.warning(f"环境变量 {env_key}={value} 不是数字, 已忽略")
        elif conf_key in ("jobs",):
            try:
                conf[conf_key] = int(value)
            except ValueError:
                logger.warning(f"环境变量 {env_key}={value} 不是整数, 已忽略")
        elif conf_key == "course_list":
            conf[conf_key] = [item.strip() for item in value.split(",") if item.strip()]
        elif conf_key in ("use_cookies", "verbose"):
            conf[conf_key] = str_to_bool(value)
        else:
            conf[conf_key] = value

    if args is not None:
        if args.username:
            conf["username"] = args.username.strip()
        if args.password:
            conf["password"] = args.password.strip()
        if args.list:
            conf["course_list"] = [item.strip() for item in args.list.split(",") if item.strip()]
        if args.speed is not None:
            conf["speed"] = float(args.speed)
        if args.jobs is not None:
            conf["jobs"] = int(args.jobs)
        if args.notopen_action:
            conf["notopen_action"] = args.notopen_action
        if args.use_cookies:
            conf["use_cookies"] = True
        if args.verbose:
            conf["verbose"] = True

    conf.setdefault("use_cookies", False)
    conf.setdefault("verbose", False)
    conf.setdefault("notopen_action", "retry")
    try:
        conf["speed"] = min(2.0, max(1.0, float(conf.get("speed", 1.0) or 1.0)))
    except (TypeError, ValueError):
        conf["speed"] = 1.0
    try:
        conf["jobs"] = max(1, int(conf.get("jobs", 4) or 4))
    except (TypeError, ValueError):
        conf["jobs"] = 4
    course_list = conf.get("course_list")
    if isinstance(course_list, str):
        conf["course_list"] = [item.strip() for item in course_list.split(",") if item.strip()]
    return conf


def init_config():
    """初始化配置"""
    args = parse_args()

    file_common: dict = {}
    file_tiku: dict = {}
    file_notify: dict = {}
    if args.config:
        file_common, file_tiku, file_notify = load_config_from_file(args.config)
        logger.info(f"已加载配置文件: {args.config}")
    else:
        logger.info("未指定配置文件, 使用命令行参数/环境变量运行")

    common_config = build_common_config(args, file_common)
    tiku_config = resolve_tiku_config(file_tiku, args, os.environ)
    # 通知: 空值(模板里的 url=)不覆盖, 且环境变量优先级高于配置文件
    notification_config = {key: value for key, value in (file_notify or {}).items()
                           if value is not None and str(value).strip()}
    notification_config.update(collect_notification_config_from_env())
    return common_config, tiku_config, notification_config



def init_chaoxing(common_config, tiku_config):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)

    # 答案缓存(题库的一部分)位置: 配置里指定后, 缓存文件也能随仓库提交而积累
    cache_file = str((tiku_config or {}).get("cache_file") or "").strip()
    if cache_file:
        CacheDAO.DEFAULT_CACHE_FILE = cache_file
    logger.info(f"答案缓存文件: {CacheDAO.DEFAULT_CACHE_FILE}")

    # 如果没有提供用户名密码，从命令行获取(非交互环境直接报错, 避免 CI 里挂死)
    if (not username or not password) and not use_cookies:
        if not stdin_is_interactive():
            raise LoginError("缺少账号或密码, 且当前是非交互环境: 请用 -u/-p 参数或 "
                             "CHAOXING_USERNAME/CHAOXING_PASSWORD 环境变量提供")
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")

    account = Account(username, password)

    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    tiku = tiku.get_tiku_from_config()  # 载入题库
    logger.info(f"题库配置: {describe_tiku_config(tiku_config)}")
    tiku.init_tiku()  # 初始化题库
    if tiku.DISABLE:
        logger.warning("题库未启用: 章节检测将只做视频/文档等任务点")
    else:
        logger.info(f"题库已启用: {tiku.name}")

    # 获取查询延迟设置

    # 检查大模型连接（如果使用的是大模型题库）
    # 根据配置文件中的 provider 判断是否为大模型题库
    provider = tiku_config.get('provider', '')
    provider_list = [name.strip() for name in str(provider).split(',') if name.strip()]
    if any(name in ['AI', 'SiliconFlow'] for name in provider_list):
        check_connection = str(tiku_config.get('check_llm_connection', 'true')).lower() == 'true'
        if check_connection:
            logger.info(f'正在验证大模型配置 (provider={provider})...')
            if not tiku.check_llm_connection():
                if stdin_is_interactive():
                    choice = input('大模型连接检查失败，无法准确答题，是否继续运行？(Y/n): ').strip().lower()
                    # 直接回车默认继续运行
                    if choice not in ('', 'y', 'yes'):
                        raise RuntimeError('用户取消运行')
                    logger.info('用户选择继续运行...')
                else:
                    # 非交互(GitHub Actions 等)环境: 不阻塞等待输入, 继续走题库回退链
                    logger.warning('大模型连接检查失败, 非交互环境将继续运行, 其余题库仍会生效')

    query_delay = tiku_config.get("delay", 0)
    try:
        query_delay = float(query_delay or 0)
    except (TypeError, ValueError):
        query_delay = 0.0

    # 实例化超星API
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=query_delay)

    return chaoxing


def export_tiku_bank(tiku_config) -> None:
    """把本次运行积累的答案缓存合并回本地题库文件(提交回仓库后, 题库会越跑越全)。

    只补新题, 不覆盖题库里已有的答案 —— 那可能是你人工核对过的。
    """
    if not str_to_bool(tiku_config.get('export_bank', 'false')):
        return
    try:
        bank_paths = split_paths(tiku_config.get('local_bank')) or [DEFAULT_BANK_PATH]
        bank = LocalBank(paths=bank_paths)
        before = bank.load()
        for issue in bank.issues:
            logger.warning(f"题库提示: {issue}")
        cache_file = tiku_config.get('cache_file') or CacheDAO.DEFAULT_CACHE_FILE
        result = bank.import_cache(cache_file)
        output = bank.save()
        logger.info(f"题库回写完成: 原 {before} 条, 新增 {result['added']} 条, "
                    f"跳过 {result['skipped']} 条(题库已有答案), 现共 {len(bank)} 条 -> {output}")
    except Exception as e:
        logger.error(f"题库回写失败: {e}")

def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }
            
            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )
            
            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any] = field(compare=False)
    result: ChapterResult = field(default=ChapterResult.PENDING, compare=False)
    tries: int = field(default=0, compare=False)

class JobProcessor:
    STOP_POINT = {"__stop__": True}

    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        if "jobs" not in config or not config["jobs"]:
            config["jobs"] = 4
        
        self.chaoxing = chaoxing
        self.course = course
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.retry_worker: threading.Thread | None = None
        self.worker_num = config["jobs"]
        self.config = config

    def _stop_task(self) -> ChapterTask:
        return ChapterTask(index=sys.maxsize, point=self.STOP_POINT)

    @staticmethod
    def _is_stop_task(task: ChapterTask) -> bool:
        return task.point.get("__stop__") is True

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        self.retry_worker = threading.Thread(target=self.retry_thread, daemon=True)
        self.retry_worker.start()

        self.task_queue.join()

        self.retry_queue.put(self._stop_task())
        for _ in self.threads:
            self.task_queue.put(self._stop_task())

        for thread in self.threads:
            thread.join()
        if self.retry_worker:
            self.retry_worker.join()


    @log_error
    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            task = self.task_queue.get()
            if self._is_stop_task(task):
                self.task_queue.task_done()
                logger.debug("Queue worker stopped")
                return

            task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed)

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {}", task.point["title"])
                    self.task_queue.task_done()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.NOT_OPEN:
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {}, 正在跳过", task.point["title"])
                        self.task_queue.task_done()
                        continue

                    task.tries += 1
                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                        , task.point["title"])
                        self.task_queue.task_done()
                        continue

                    # self.wait_queue.put(task)
                    self.retry_queue.put(task)

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("Retrying task {} ({}/{} attempts)", task.point["title"], task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("Max retries reached for task: {}", task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        continue
                    self.retry_queue.put(task)

                case _:
                    logger.error("Invalid task state {} for task {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()

    @log_error
    def retry_thread(self):
        while True:
            task = self.retry_queue.get()
            if self._is_stop_task(task):
                self.retry_queue.task_done()
                return
            self.task_queue.put(task)
            # task_done is not called when a task failed and needs to be retried so if is reinserted into the queue,
            # the task num will increase by one and become more than the real task number
            self.task_queue.task_done()
            self.retry_queue.task_done()
            time.sleep(1) # TODO: Replace with a configurable wait time


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float) -> ChapterResult:
    """处理单个章节"""
    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 已经默认处理空任务，此处不需要判断
    if not jobs:
        pass

    # TODO: 个别章节很恶心，多到5个点，可以并行处理，将来会让不同课程不同章节的所有任务点共享一个队列，从而实现全局并行
    job_results:list[StudyResult]=[]
    with ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(lambda job: process_job(chaoxing, course, job, job_info, speed), jobs):
            job_results.append(result)
    
    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    return ChapterResult.SUCCESS



def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())

    tasks=[]

    for i, point in enumerate(point_list["points"]):
        task = ChapterTask(point=point, index=i)
        tasks.append(task)
    p = JobProcessor(chaoxing, course, tasks, config)
    p.run()


    tqdm.format_sizeof = _old_format_sizeof

def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        if not stdin_is_interactive():
            # 非交互环境(GitHub Actions 等)不能等待输入, 默认学习全部课程
            logger.warning("未指定课程ID且当前是非交互环境, 将学习全部课程(可在 secrets 里配置 CHAOXING_COURSE_LIST)")
            return all_course
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e
    course_list = [str(course_id).strip() for course_id in course_list if str(course_id).strip()]
    if not course_list:
        logger.warning("未指定课程ID，将学习全部课程")
        return all_course

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    if not course_task:
        raise InputFormatError(f"未找到指定课程ID: {', '.join(course_list)}")
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def main():
    """主程序入口"""
    tiku_config: dict = {}
    notification = None
    try:
        # 初始化配置
        common_config, tiku_config, notification_config = init_config()
        configure_console_logger("DEBUG" if common_config.get("verbose", False) else "INFO")
        
        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        
        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()
        
        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))
        
        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
        for course in course_task:
            process_course(chaoxing, course, common_config)
        
        logger.info("所有课程学习任务已完成")
        notification.send("chaoxing : 所有课程学习任务已完成")
        
    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            if notification is not None:
                notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e
    finally:
        # 即使中途出错, 也把已经搜到的答案落盘/合并进题库, 不浪费这一轮的成果
        try:
            export_tiku_bank(tiku_config)
        except Exception as e:
            logger.error(f"题库回写异常: {e}")


if __name__ == "__main__":
    main()
