"""Local backend for the personal 秋招雷达 site.

The browser talks only to this process. It serves the UI, stores jobs in SQLite,
refreshes public source pages periodically, and invokes the user's configured
Codex/CCSwitch provider for resume assistance.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "qiuzhao.db"
HOST = "::"
PORT = 5500
DATA_DIR = ROOT / "data"
XIAOZHAO_SITES_FILE = DATA_DIR / "xiaozhao_sites.json"
XIAOZHAO_BATCHES_FILE = DATA_DIR / "xiaozhao_batches.json"
JOBPRO_COMPANIES_FILE = DATA_DIR / "jobpro_companies.json"


def load_dotenv_local() -> None:
    """Tiny optional .env loader (no third-party dependency)."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv_local()

BASE_URL = os.environ.get("RELAXY_BASE_URL", "https://www.relaxycode.com/v1").rstrip("/")
MODEL = os.environ.get("RELAXY_MODEL", "gpt-5.6-sol")
REFRESH_SECONDS = 30 * 60
EXTRA_POOL_ENABLED = os.environ.get("EXTRA_POOL_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
try:
    EXTRA_BATCH_SIZE = max(1, int(os.environ.get("EXTRA_BATCH_SIZE", "25")))
except (TypeError, ValueError):
    EXTRA_BATCH_SIZE = 25
ANYSEARCH_ENABLED = os.environ.get("ANYSEARCH_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
ANYSEARCH_API_KEY = os.environ.get("ANYSEARCH_API_KEY", "").strip()
JOBPRO_ENABLED = os.environ.get("JOBPRO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
try:
    JOBPRO_BATCH_SIZE = max(1, int(os.environ.get("JOBPRO_BATCH_SIZE", "5")))
except (TypeError, ValueError):
    JOBPRO_BATCH_SIZE = 5
try:
    JOBPRO_TIMEOUT_SECONDS = max(10, int(os.environ.get("JOBPRO_TIMEOUT_SECONDS", "90")))
except (TypeError, ValueError):
    JOBPRO_TIMEOUT_SECONDS = 90
JOBPRO_COMPANIES_FILTER = [item.strip().lower() for item in os.environ.get("JOBPRO_COMPANIES", "").split(",") if item.strip()]
JOBPRO_HIGH_CAP_KEYS = {
    item.strip().lower() for item in os.environ.get(
        "JOBPRO_HIGH_CAP_COMPANIES",
        "unitree,agibot,horizonrobotics,cambricon,galaxyuniversal",
    ).split(",") if item.strip()
}
# Companies whose recruiting is WeChat/Liepin-chat mediated: job-pro can only
# surface the apply page, there is no automated submission endpoint.
JOBPRO_EXTERNAL_ONLY = {"hikvision", "cicc", "cainiao", "webank", "unitree"}
# Local build of the job-pro feature branch that implements the apply flow
# (the published npm package currently lacks the `apply` verb).
JOBPRO_CLI_PATH = Path(os.environ.get(
    "JOBPRO_CLI",
    str(ROOT.parents[1] / "tmp" / "job-pro" / "cli" / "dist" / "index.js"),
))
JOBPRO_DIR = Path.home() / ".jobpro"
JOBPRO_PROFILE_PATH = Path(os.environ.get("JOB_PRO_PROFILE_PATH", str(JOBPRO_DIR / "profile.json")))
MAX_BODY = 16 * 1024 * 1024
MAX_MESSAGES = 20
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
TRACKING_STATUSES = {"wishlist", "applied", "written", "interview1", "interview2", "interview3", "hr", "offer", "rejected", "closed"}
TRACKING_LABELS = {
    "wishlist": "想投递", "applied": "已投递", "written": "笔试",
    "interview1": "一面", "interview2": "二面", "interview3": "三面",
    "hr": "HR面", "offer": "Offer", "rejected": "已拒绝", "closed": "已终止",
}

# A source page often contains broad campus-recruiting navigation links. Keep
# only titles that have a clear relationship to this resume's target roles.
ROLE_TERMS = (
    "嵌入式", "机器人", "固件", "驱动", "底层", "单片机", "MCU", "BSP", "RTOS",
    "ROS", "Jetson", "电控", "电机控制", "运动控制", "硬件研发", "硬件工程",
    "电子研发", "算法工程师", "测试开发", "软件工程", "开发工程师",
    "embedded", "firmware", "robotics", "robot", "device driver", "motor control",
)
JOB_TITLE_TERMS = ("工程师", "研发", "开发", "职位", "岗位", "校招", "招聘", "developer", "engineer", "research", "firmware", "embedded")
NOISE_TERMS = ("首页", "关于我们", "了解更多", "职位搜索", "招聘会", "宣讲会", "福利", "新闻")
MARKETING_TERMS = ("产品", "解决方案", "商城", "培训", "课程", "挑战高薪", "年薪", "robotic arms", "r1 robotic", "go2", "product", "solution", "training", "course")
# Aggregators expose broad role categories. Drop directions that are clearly
# outside this resume's embedded/robotics/hardware target while keeping general
# engineering roles such as firmware, Linux, motor control, and communications.
EXCLUDE_TERMS = (
    "web开发", "web前端", "前端开发", "前端工程", "游戏开发", "游戏工程",
    "数据库开发", "大数据开发", "移动开发", "移动前端", "android应用",
    "ios开发", "flutter", "java开发", "php开发", "golang开发",
    "运维工程", "软件测试", "测试开发", "产品经理", "内容运营", "新媒体",
    "销售", "市场专员", "客服",
)
# Directions inferred by the custom matcher that are pure software / AI and
# intentionally outside this resume's embedded & robotics focus.
IRRELEVANT_DIRECTIONS = {"软件研发", "后端开发", "算法与AI"}
SOURCE_EMPTY_ERROR = "页面已访问，但未解析到匹配岗位"
JOB_PATH_HINTS = {
    "dji": ("campus", "career", "job", "position", "recruit"),
    "unitree": ("career", "job", "recruit", "campus", "join", "position", "招聘"),
    "hikvision": ("campus", "career", "job", "position", "recruit"),
    "xiaomi": ("campus", "career", "job", "position", "recruit"),
    "inovance": ("campus", "career", "job", "position", "recruit", "zhiye"),
    "iflytek": ("campus", "career", "job", "position", "recruit", "zhiye"),
    "huawei": ("campus", "career", "job", "position", "recruit", "portal"),
    "byd": ("campus", "career", "job", "position", "recruit"),
    "zte": ("campus", "career", "job", "position", "recruit"),
    "alibaba": ("campus", "career", "job", "position", "recruit"),
    "baidu": ("campus", "career", "job", "position", "recruit", "social-list"),
    "bytedance": ("campus", "career", "job", "position", "recruit"),
    "tencent": ("campus", "career", "job", "position", "recruit"),
    "meituan": ("campus", "career", "job", "position", "recruit"),
    "yingjiesheng": ("job", "campus", "zhaopin", "recruit", "2026", "2027"),
    "nowcoder": ("career", "job", "campus", "recruit", "zhaopin"),
    "guopin": ("job", "campus", "recruit", "zhaopin"),
    "campus": ("job", "campus", "recruit", "zhaopin", "employment"),
}
# Newly added sources use these generic recruitment-path hints unless they have
# an entry above, so expanding SOURCES does not silently filter every link.
DEFAULT_JOB_PATH_HINTS = (
    "join", "career", "careers", "job", "jobs", "position", "recruit",
    "campus", "zhaopin", "employment", "hiring", "talent",
)
REFRESH_LOCK = threading.Lock()

PROFILE = {
    "version": "2026-08-31",
    "targetRoles": ["嵌入式研发", "机器人研发"],
    "major": "计算机科学与技术",
    "skills": [
        "C/C++", "Python", "STM32", "ESP32-S3", "FreeRTOS", "Linux", "ROS2",
        "Jetson", "UART", "SPI", "I2C", "CAN", "SBUS", "BLE", "WiFi",
        "TCP", "WebSocket",
    ],
    "keywords": ["机器人控制", "运动控制", "嵌入式系统", "硬件协同开发"],
    "preferredCities": ["深圳", "杭州", "北京", "成都"],
}


def load_profile_file() -> dict | None:
    """Load an optional profile.json next to server.py so the packaged tool can
    be pointed at a different resume without editing source code."""
    path = ROOT / "profile.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return normalize_profile(data, PROFILE)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


# Official career pages are preferred. Aggregator pages are retained as backup
# sources and are checked without bypassing login, CAPTCHA, or access controls.
SOURCES = [
    {"id": "dji", "name": "大疆校招", "kind": "official", "company": "大疆", "url": "https://careers.dji.com/zh-CN/campus", "priority": 100},
    {"id": "unitree", "name": "宇树科技招聘", "kind": "official", "company": "宇树科技", "url": "https://unitree.zhiye.com", "priority": 100},
    {"id": "hikvision", "name": "海康威视校招", "kind": "official", "company": "海康威视", "url": "https://campushr.hikvision.com", "priority": 100},
    {"id": "xiaomi", "name": "小米招聘", "kind": "official", "company": "小米", "url": "https://hr.xiaomi.com", "priority": 100},
    {"id": "inovance", "name": "汇川技术校招", "kind": "official", "company": "汇川技术", "url": "https://inovance.zhiye.com", "priority": 100},
    {"id": "iflytek", "name": "科大讯飞校招", "kind": "official", "company": "科大讯飞", "url": "https://iflytek.zhiye.com", "priority": 100},
    {"id": "huawei", "name": "华为校招", "kind": "official", "company": "华为", "url": "https://career.huawei.com/reccampportal/portal5/index.html", "priority": 100},
    {"id": "byd", "name": "比亚迪招聘", "kind": "official", "company": "比亚迪", "url": "https://job.byd.com/", "priority": 100},
    {"id": "zte", "name": "中兴通讯校招", "kind": "official", "company": "中兴通讯", "url": "https://job.zte.com.cn/", "priority": 100},
    {"id": "alibaba", "name": "阿里巴巴校招", "kind": "official", "company": "阿里巴巴", "url": "https://campus.alibaba.com/", "priority": 100},
    {"id": "baidu", "name": "百度校招", "kind": "official", "company": "百度", "url": "https://talent.baidu.com/jobs/social-list", "priority": 100},
    {"id": "bytedance", "name": "字节跳动校招", "kind": "official", "company": "字节跳动", "url": "https://jobs.bytedance.com/campus/", "priority": 100},
    {"id": "tencent", "name": "腾讯校招", "kind": "official", "company": "腾讯", "url": "https://career.tencent.com/", "priority": 100},
    {"id": "meituan", "name": "美团校招", "kind": "official", "company": "美团", "url": "https://campus.meituan.com/", "priority": 100},
    {"id": "yingjiesheng", "name": "应届生求职网", "kind": "aggregator", "company": "", "url": "https://www.yingjiesheng.com/", "priority": 50},
    {"id": "nowcoder", "name": "牛客校招", "kind": "aggregator", "company": "", "url": "https://www.nowcoder.com/careers", "priority": 50},
    {"id": "guopin", "name": "国聘", "kind": "aggregator", "company": "", "url": "https://www.iguopin.com/", "priority": 50},
    {"id": "espressif", "name": "乐鑫科技招聘", "kind": "official", "company": "乐鑫科技", "url": "https://www.espressif.com/zh-hans/join-us/job-search", "priority": 100},
    {"id": "gigadevice", "name": "兆易创新招聘", "kind": "official", "company": "兆易创新", "url": "https://app.mokahr.com/campus-recruitment/gigadevice/92215", "priority": 100},
    {"id": "rockchip", "name": "瑞芯微招聘", "kind": "official", "company": "瑞芯微", "url": "https://hr.rock-chips.com/campus-recruitment", "priority": 100},
    {"id": "allwinner", "name": "全志科技招聘", "kind": "official", "company": "全志科技", "url": "https://campus.allwinnertech.com", "priority": 100},
    {"id": "horizon", "name": "地平线招聘", "kind": "official", "company": "地平线", "url": "https://horizon-campus.hotjob.cn/", "priority": 100},
    {"id": "cambricon", "name": "寒武纪招聘", "kind": "official", "company": "寒武纪", "url": "https://www.cambricon.com/recruit/", "priority": 100},
    {"id": "ubtech", "name": "优必选招聘", "kind": "official", "company": "优必选", "url": "https://ubtrobot.zhiye.com/campus", "priority": 100},
    {"id": "deeprobotics", "name": "云深处科技招聘", "kind": "official", "company": "云深处科技", "url": "https://app135149.eapps.dingtalkcloud.com/campus-recruitment/yunshenchu/", "priority": 100},
    {"id": "fourier", "name": "傅利叶智能招聘", "kind": "official", "company": "傅利叶智能", "url": "https://www.fourierintelligence.com/careers", "priority": 100},
    {"id": "agibot", "name": "智元机器人招聘", "kind": "official", "company": "智元机器人", "url": "https://agirobot.jobs.feishu.cn/s/y8YSmRPj5Xk", "priority": 100},
    {"id": "dreame", "name": "追觅科技招聘", "kind": "official", "company": "追觅科技", "url": "https://dreame.zhiye.com/campus/jobs", "priority": 100},
    {"id": "roborock", "name": "石头科技招聘", "kind": "official", "company": "石头科技", "url": "https://roborock.zhiye.com/campus", "priority": 100},
    {"id": "ecovacs", "name": "科沃斯招聘", "kind": "official", "company": "科沃斯", "url": "https://www.ecovacs.com/join", "priority": 100},
    {"id": "geekplus", "name": "极智嘉招聘", "kind": "official", "company": "极智嘉", "url": "https://app.mokahr.com/campus-recruitment/geekplus/165879", "priority": 100},
    {"id": "hairobotics", "name": "海柔创新招聘", "kind": "official", "company": "海柔创新", "url": "https://hairobotics.zhiye.com/campus", "priority": 100},
    {"id": "dobot", "name": "越疆机器人招聘", "kind": "official", "company": "越疆机器人", "url": "https://dobot.zhiye.com/campus/jobs", "priority": 100},
    {"id": "jaka", "name": "节卡机器人招聘", "kind": "official", "company": "节卡机器人", "url": "https://www.jaka.com/zh/home/", "priority": 100},
    {"id": "xpeng", "name": "小鹏汽车招聘", "kind": "official", "company": "小鹏汽车", "url": "https://xiaopeng.jobs.feishu.cn/campus", "priority": 100},
    {"id": "lixiang", "name": "理想汽车招聘", "kind": "official", "company": "理想汽车", "url": "https://www.lixiang.com/join", "priority": 100},
    {"id": "nio", "name": "蔚来招聘", "kind": "official", "company": "蔚来", "url": "https://campus.nio.com/", "priority": 100},
    {"id": "weride", "name": "文远知行招聘", "kind": "official", "company": "文远知行", "url": "https://app.mokahr.com/campus_apply/jingchi/2137", "priority": 100},
    {"id": "ponyai", "name": "小马智行招聘", "kind": "official", "company": "小马智行", "url": "https://campus.pony.ai/", "priority": 100},
    {"id": "momenta", "name": "Momenta招聘", "kind": "official", "company": "Momenta", "url": "https://momenta.jobs.feishu.cn/campus", "priority": 100},
    {"id": "oppo", "name": "OPPO招聘", "kind": "official", "company": "OPPO", "url": "https://careers.oppo.com/university/oppo/campus", "priority": 100},
    {"id": "vivo", "name": "vivo招聘", "kind": "official", "company": "vivo", "url": "https://hr-campus.vivo.com/", "priority": 100},
    {"id": "honor", "name": "荣耀招聘", "kind": "official", "company": "荣耀", "url": "https://www.honor.com/cn/career/", "priority": 100},
    {"id": "lenovo", "name": "联想招聘", "kind": "official", "company": "联想", "url": "https://talent.lenovo.com.cn/position?projectType=3", "priority": 100},
    {"id": "transsion", "name": "传音控股招聘", "kind": "official", "company": "传音控股", "url": "https://transsion.zhiye.com/Campus", "priority": 100},
    {"id": "anker", "name": "安克创新招聘", "kind": "official", "company": "安克创新", "url": "https://www.anker.com/careers", "priority": 100},
    {"id": "quectel", "name": "移远通信招聘", "kind": "official", "company": "移远通信", "url": "https://talent.quectel.com/campus", "priority": 100},
    {"id": "fibocom", "name": "广和通招聘", "kind": "official", "company": "广和通", "url": "https://fibocom.zhiye.com/", "priority": 100},
    {"id": "supcon", "name": "中控技术招聘", "kind": "official", "company": "中控技术", "url": "https://app.mokahr.com/campus-recruitment/supcon/148189", "priority": 100},
    {"id": "estun", "name": "埃斯顿招聘", "kind": "official", "company": "埃斯顿", "url": "https://estun1.zhiye.com/campus", "priority": 100},
    {"id": "topstar", "name": "拓斯达招聘", "kind": "official", "company": "拓斯达", "url": "https://www.topstarltd.com/lang-cn/recruitinglist/006002003.html", "priority": 100},
    {"id": "leadshine", "name": "雷赛智能招聘", "kind": "official", "company": "雷赛智能", "url": "https://www.leisai.com/", "priority": 100},
    {"id": "h3c", "name": "新华三招聘", "kind": "official", "company": "新华三", "url": "https://www.h3c.com/cn/About_Us/Join_Us/", "priority": 100},
    {"id": "ruijie", "name": "锐捷网络招聘", "kind": "official", "company": "锐捷网络", "url": "https://www.ruijie.com.cn/join", "priority": 100},
    {"id": "sensetime", "name": "商汤科技招聘", "kind": "official", "company": "商汤科技", "url": "https://hr.sensetime.com/", "priority": 100},
    {"id": "megvii", "name": "旷视科技招聘", "kind": "official", "company": "旷视科技", "url": "https://app.mokahr.com/campus_apply/megviihr/38642", "priority": 100},
    {"id": "cloudwalk", "name": "云从科技招聘", "kind": "official", "company": "云从科技", "url": "https://www.cloudwalk.com/join", "priority": 100},
    {"id": "zhipuai", "name": "智谱AI招聘", "kind": "official", "company": "智谱AI", "url": "https://www.zhipuai.cn/join", "priority": 100},
    {"id": "moonshot", "name": "月之暗面招聘", "kind": "official", "company": "月之暗面", "url": "https://www.moonshot.cn/join", "priority": 100},
    {"id": "minimax", "name": "MiniMax招聘", "kind": "official", "company": "MiniMax", "url": "https://www.minimax.io/careers", "priority": 100},
    {"id": "catl", "name": "宁德时代招聘", "kind": "official", "company": "宁德时代", "url": "https://talent.catl.com/", "priority": 100},
    {"id": "eve", "name": "亿纬锂能招聘", "kind": "official", "company": "亿纬锂能", "url": "https://www.evebattery.com/join", "priority": 100},
    {"id": "sunwoda", "name": "欣旺达招聘", "kind": "official", "company": "欣旺达", "url": "https://www.sunwoda.com/join", "priority": 100},
    {"id": "midea", "name": "美的招聘", "kind": "official", "company": "美的", "url": "https://careers.midea.com/", "priority": 100},
    {"id": "gree", "name": "格力招聘", "kind": "official", "company": "格力", "url": "https://www.gree.com.cn/join", "priority": 100},
    {"id": "haier", "name": "海尔招聘", "kind": "official", "company": "海尔", "url": "https://maker.haier.net/smart_home", "priority": 100},
    {"id": "tcl", "name": "TCL招聘", "kind": "official", "company": "TCL", "url": "https://www.tcl.com/careers", "priority": 100},
    {"id": "csot", "name": "华星光电招聘", "kind": "official", "company": "华星光电", "url": "https://www.szcsot.com/join", "priority": 100},
    {"id": "campus", "name": "高校就业信息网", "kind": "aggregator", "company": "", "url": "https://www.ncss.cn/", "priority": 40},
    {"id": "zhipin", "name": "BOSS直聘", "kind": "aggregator", "company": "", "url": "https://www.zhipin.com/", "priority": 40},
    {"id": "liepin", "name": "猎聘", "kind": "aggregator", "company": "", "url": "https://www.liepin.com/", "priority": 40},
    {"id": "zhaopin", "name": "智联招聘", "kind": "aggregator", "company": "", "url": "https://www.zhaopin.com/", "priority": 40},
    {"id": "shixiseng", "name": "实习僧", "kind": "aggregator", "company": "", "url": "https://www.shixiseng.com/", "priority": 40},
    {"id": "lagou", "name": "拉勾招聘", "kind": "aggregator", "company": "", "url": "https://www.lagou.com/", "priority": 40},
]

# Direction tags let the background refresh rank sources against the current
# resume profile instead of treating every source equally. Untagged sources
# still fall back to name/company matching.
SOURCE_TAGS = {
    "dji": ["机器人", "嵌入式", "硬件"],
    "unitree": ["机器人", "嵌入式", "运动控制"],
    "ubtech": ["机器人", "嵌入式", "运动控制"],
    "deeprobotics": ["机器人", "嵌入式", "运动控制"],
    "fourier": ["机器人", "嵌入式", "运动控制"],
    "agibot": ["机器人", "嵌入式", "运动控制"],
    "dreame": ["机器人", "嵌入式", "硬件"],
    "roborock": ["机器人", "嵌入式"],
    "ecovacs": ["机器人", "嵌入式"],
    "geekplus": ["机器人", "嵌入式", "运动控制"],
    "hairobotics": ["机器人", "嵌入式"],
    "dobot": ["机器人", "嵌入式", "运动控制"],
    "jaka": ["机器人", "嵌入式", "运动控制"],
    "estun": ["机器人", "运动控制", "工业控制"],
    "topstar": ["机器人", "运动控制", "工业控制"],
    "espressif": ["嵌入式", "硬件", "芯片", "物联网"],
    "gigadevice": ["嵌入式", "芯片"],
    "rockchip": ["嵌入式", "芯片"],
    "allwinner": ["嵌入式", "芯片"],
    "horizon": ["嵌入式", "芯片", "自动驾驶"],
    "cambricon": ["嵌入式", "芯片"],
    "hikvision": ["嵌入式", "硬件"],
    "xiaomi": ["嵌入式", "硬件"],
    "inovance": ["嵌入式", "运动控制", "工业控制"],
    "iflytek": ["机器人", "嵌入式", "AI"],
    "huawei": ["嵌入式", "芯片", "通信"],
    "byd": ["嵌入式", "汽车电子", "BMS"],
    "zte": ["嵌入式", "通信"],
    "xpeng": ["嵌入式", "自动驾驶"],
    "lixiang": ["嵌入式", "自动驾驶"],
    "nio": ["嵌入式", "自动驾驶"],
    "weride": ["嵌入式", "自动驾驶"],
    "ponyai": ["嵌入式", "自动驾驶"],
    "momenta": ["嵌入式", "自动驾驶"],
    "quectel": ["嵌入式", "物联网", "通信"],
    "fibocom": ["嵌入式", "物联网"],
    "supcon": ["嵌入式", "工业控制"],
    "leadshine": ["嵌入式", "运动控制"],
    "h3c": ["嵌入式", "通信"],
    "ruijie": ["嵌入式", "通信"],
    "sensetime": ["嵌入式", "AI", "边缘"],
    "megvii": ["嵌入式", "AI", "边缘"],
    "cloudwalk": ["嵌入式", "AI"],
    "zhipuai": ["AI"],
    "moonshot": ["AI"],
    "minimax": ["AI"],
    "catl": ["嵌入式", "BMS", "新能源"],
    "eve": ["嵌入式", "BMS"],
    "sunwoda": ["嵌入式", "BMS"],
    "midea": ["嵌入式", "硬件"],
    "gree": ["嵌入式", "硬件"],
    "haier": ["嵌入式", "硬件"],
    "tcl": ["嵌入式", "硬件"],
    "csot": ["嵌入式", "硬件"],
    "oppo": ["嵌入式", "硬件"],
    "vivo": ["嵌入式", "硬件"],
    "honor": ["嵌入式", "硬件"],
    "lenovo": ["嵌入式", "硬件"],
    "transsion": ["嵌入式", "硬件"],
    "anker": ["嵌入式", "硬件"],
    "alibaba": ["AI"],
    "baidu": ["AI"],
    "bytedance": ["AI"],
    "tencent": ["AI"],
    "meituan": ["AI"],
}

# Only these sources are treated as campus/autumn-recruiting openings. Other
# sources are labelled social/日常招聘 and are hidden from the default list.
CAMPUS_SOURCE_IDS = {
    "dji", "huawei", "hikvision", "inovance", "iflytek", "zte",
    "alibaba", "bytedance", "tencent", "meituan",
    "allwinner", "gigadevice", "ubtech", "deeprobotics", "quectel", "horizon",
    "unitree", "espressif", "rockchip", "dreame", "roborock",
    "geekplus", "hairobotics", "dobot",
    "supcon", "fibocom", "xpeng", "nio", "jaka",
    "estun", "topstar", "weride", "ponyai", "momenta", "oppo", "vivo",
    "honor", "sensetime", "megvii", "catl", "midea", "haier", "lenovo", "transsion",
    "nowcoder", "campus", "yingjiesheng",
}
CAMPUS_SOURCE_NAMES = {source["name"] for source in SOURCES if source["id"] in CAMPUS_SOURCE_IDS}


AGGREGATOR_HOST_HINTS = (
    "51job.com", "yingjiesheng.com", "iguopin.com", "zhipin.com", "liepin.com",
    "zhaopin.com", "shixiseng.com", "lagou.com", "nowcoder.com", "bysjy.com.cn",
    "ncss.cn",
)


def _normalized_source_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{parts.path.rstrip('/').lower()}"


def _kind_for_source_url(url: str) -> str:
    key = _normalized_source_url(url)
    if any(hint in key for hint in AGGREGATOR_HOST_HINTS):
        return "aggregator"
    return "official"


def _read_json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def load_xiaozhao_sites() -> list[dict]:
    """Curated career-page pool exported from the xiaozhao-radar project."""
    if not EXTRA_POOL_ENABLED:
        return []
    core_keys = {_normalized_source_url(source["url"]) for source in SOURCES}
    sites: list[dict] = []
    seen: set[str] = set()
    for item in _read_json_list(XIAOZHAO_SITES_FILE):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        company = str(item.get("company") or item.get("cat") or "").strip()
        key = _normalized_source_url(url)
        if not url or not company or not key or key in core_keys or key in seen:
            continue
        seen.add(key)
        sites.append({
            "id": str(item.get("id") or "").strip() or f"xz-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}",
            "name": str(item.get("name") or f"{company}（校招雷达源）").strip()[:80],
            "kind": str(item.get("kind") or _kind_for_source_url(url)).strip(),
            "company": company,
            "url": url,
            "priority": int(item.get("priority") or 20),
            "industry": str(item.get("industry") or "").strip(),
            "campus": bool(item.get("campus", True)),
            "rendered": bool(item.get("rendered", True)),
            "pool": "xiaozhao-sites",
        })
    return sites


def load_xiaozhao_batches() -> list[dict]:
    """Weekly aggregated campus-batch entries (company-level URLs)."""
    if not EXTRA_POOL_ENABLED:
        return []
    core_keys = {_normalized_source_url(source["url"]) for source in SOURCES}
    batches: list[dict] = []
    seen: set[str] = set()
    for item in _read_json_list(XIAOZHAO_BATCHES_FILE):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        company = str(item.get("company") or "").strip()
        key = _normalized_source_url(url)
        if not url or not company or not key or key in core_keys or key in seen:
            continue
        seen.add(key)
        batches.append({
            "id": str(item.get("id") or "").strip() or f"xb-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}",
            "name": str(item.get("name") or company).strip()[:80],
            "kind": str(item.get("kind") or _kind_for_source_url(url)).strip(),
            "company": company,
            "url": url,
            "priority": int(item.get("priority") or 15),
            "industry": str(item.get("industry") or "").strip(),
            "categories": str(item.get("categories") or "").strip()[:400],
            "locations": str(item.get("locations") or "").strip()[:200],
            "deadline": str(item.get("deadline") or "").strip()[:80],
            "batch": str(item.get("batch") or "校招批次").strip()[:60],
            "campus": bool(item.get("campus", True)),
            "rendered": bool(item.get("rendered", True)),
            "pool": "xiaozhao-batches",
        })
    return batches


def extra_sources() -> list[dict]:
    sites = load_xiaozhao_sites()
    site_keys = {_normalized_source_url(item["url"]) for item in sites}
    batches = [
        item for item in load_xiaozhao_batches()
        if _normalized_source_url(str(item.get("url") or "")) not in site_keys
    ]
    return sites + batches


def all_sources() -> list[dict]:
    if not EXTRA_POOL_ENABLED:
        return SOURCES
    return [*SOURCES, *extra_sources()]


def jobpro_sources() -> list[dict]:
    """Official-API source definitions backed by the job-pro CLI (50 companies)."""
    if not JOBPRO_ENABLED:
        return []
    payload = _read_json_list(JOBPRO_COMPANIES_FILE)
    result: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not key:
            continue
        if JOBPRO_COMPANIES_FILTER and key not in JOBPRO_COMPANIES_FILTER:
            continue
        company = str(item.get("company") or "").strip()
        if not company:
            label = str(item.get("label") or "").strip()
            company = label.split("/", 1)[1].strip() if "/" in label else label
        base = str(item.get("source") or "").strip()
        url = base if base.startswith(("http://", "https://")) else (f"https://{base}" if base else "")
        if not url or not company:
            continue
        result.append({
            "id": f"jp-{key}",
            "name": f"{company}（官方API）",
            "company": company,
            "url": url,
            "kind": "official-api",
            "priority": 95,
            "campus": True,
            "rendered": False,
            "key": key,
        })
    return result


SEED_JOBS = [
    ("嵌入式软件工程师（校招）", "大疆", "深圳", "18-28K", "嵌入式研发", 96, 18, 28, ["C/C++", "STM32", "FreeRTOS"], "https://careers.dji.com/zh-CN/campus", "大疆校招", 100),
    ("机器人嵌入式工程师", "宇树科技", "杭州", "20-32K", "机器人研发", 94, 20, 32, ["ROS2", "Linux", "CAN"], "https://unitree.zhiye.com", "宇树科技招聘", 100),
    ("嵌入式开发工程师", "海康威视", "杭州", "15-25K", "嵌入式研发", 90, 15, 25, ["C++", "ARM", "网络协议"], "https://campushr.hikvision.com", "海康威视校招", 100),
    ("智能硬件研发工程师", "小米", "北京", "18-30K", "嵌入式研发", 87, 18, 30, ["ESP32", "WiFi/BLE", "硬件调试"], "https://hr.xiaomi.com", "小米招聘", 100),
    ("嵌入式软件工程师", "汇川技术", "深圳", "15-24K", "嵌入式研发", 84, 15, 24, ["STM32", "电机控制", "UART"], "https://inovance.zhiye.com", "汇川技术校招", 100),
    ("机器人软件工程师", "科大讯飞", "成都", "18-30K", "机器人研发", 80, 18, 30, ["Python", "Jetson", "机器学习"], "https://iflytek.zhiye.com", "科大讯飞校招", 100),
    ("嵌入式软件工程师", "乐鑫科技", "上海", "待确认", "嵌入式研发", 68, 0, 0, ["ESP32", "FreeRTOS", "WiFi/BLE"], "https://www.espressif.com/zh-hans/join-us/job-search", "乐鑫科技招聘", 100),
    ("嵌入式软件工程师（MCU）", "兆易创新", "北京", "待确认", "嵌入式研发", 66, 0, 0, ["MCU", "ARM", "C/C++"], "https://app.mokahr.com/campus-recruitment/gigadevice/92215", "兆易创新招聘", 100),
    ("嵌入式软件工程师（SoC）", "瑞芯微", "福州", "待确认", "嵌入式研发", 65, 0, 0, ["Linux", "SoC", "驱动"], "https://hr.rock-chips.com/campus-recruitment", "瑞芯微招聘", 100),
    ("嵌入式软件工程师（Linux）", "全志科技", "珠海", "待确认", "嵌入式研发", 64, 0, 0, ["Linux", "SoC", "C"], "https://campus.allwinnertech.com", "全志科技招聘", 100),
    ("嵌入式软件工程师（智能驾驶）", "地平线", "北京", "待确认", "嵌入式研发", 66, 0, 0, ["BSP", "Linux", "C++"], "https://horizon-campus.hotjob.cn/", "地平线招聘", 100),
    ("嵌入式软件工程师（AI芯片）", "寒武纪", "北京", "待确认", "嵌入式研发", 63, 0, 0, ["Linux", "驱动", "C++"], "https://www.cambricon.com/recruit/", "寒武纪招聘", 100),
    ("机器人控制软件工程师", "优必选", "深圳", "待确认", "机器人研发", 70, 0, 0, ["ROS2", "运动控制", "C++"], "https://ubtrobot.zhiye.com/campus", "优必选招聘", 100),
    ("机器人嵌入式工程师", "云深处科技", "杭州", "待确认", "机器人研发", 70, 0, 0, ["ROS2", "CAN", "Linux"], "https://app135149.eapps.dingtalkcloud.com/campus-recruitment/yunshenchu/", "云深处科技招聘", 100),
    ("机器人软件工程师", "傅利叶智能", "上海", "待确认", "机器人研发", 69, 0, 0, ["ROS2", "运动控制", "Python"], "https://www.fourierintelligence.com/careers", "傅利叶智能招聘", 100),
    ("机器人嵌入式工程师", "智元机器人", "上海", "待确认", "机器人研发", 70, 0, 0, ["ROS2", "Linux", "C++"], "https://agirobot.jobs.feishu.cn/s/y8YSmRPj5Xk", "智元机器人招聘", 100),
    ("嵌入式软件工程师", "追觅科技", "苏州", "待确认", "嵌入式研发", 63, 0, 0, ["STM32", "BLE", "RTOS"], "https://dreame.zhiye.com/campus/jobs", "追觅科技招聘", 100),
    ("机器人嵌入式工程师", "石头科技", "北京", "待确认", "机器人研发", 67, 0, 0, ["ROS2", "嵌入式", "Linux"], "https://roborock.zhiye.com/campus", "石头科技招聘", 100),
    ("机器人软件工程师", "科沃斯", "苏州", "待确认", "机器人研发", 66, 0, 0, ["ROS", "嵌入式", "C++"], "https://www.ecovacs.com/join", "科沃斯招聘", 100),
    ("机器人控制软件工程师", "极智嘉", "北京", "待确认", "机器人研发", 68, 0, 0, ["ROS2", "运动控制", "C++"], "https://app.mokahr.com/campus-recruitment/geekplus/165879", "极智嘉招聘", 100),
    ("机器人嵌入式工程师", "海柔创新", "深圳", "待确认", "机器人研发", 68, 0, 0, ["ROS2", "CAN", "Linux"], "https://hairobotics.zhiye.com/campus", "海柔创新招聘", 100),
    ("机器人控制软件工程师", "越疆机器人", "深圳", "待确认", "机器人研发", 67, 0, 0, ["运动控制", "ROS", "C++"], "https://dobot.zhiye.com/campus/jobs", "越疆机器人招聘", 100),
    ("机器人软件工程师", "节卡机器人", "上海", "待确认", "机器人研发", 66, 0, 0, ["ROS2", "运动控制", "Linux"], "https://www.jaka.com/zh/home/", "节卡机器人招聘", 100),
    ("嵌入式软件工程师（智能驾驶）", "小鹏汽车", "广州", "待确认", "嵌入式研发", 65, 0, 0, ["自动驾驶", "C++", "Linux"], "https://xiaopeng.jobs.feishu.cn/campus", "小鹏汽车招聘", 100),
    ("嵌入式软件工程师", "理想汽车", "北京", "待确认", "嵌入式研发", 64, 0, 0, ["Linux", "汽车电子", "C++"], "https://www.lixiang.com/join", "理想汽车招聘", 100),
    ("嵌入式软件工程师", "蔚来", "上海", "待确认", "嵌入式研发", 64, 0, 0, ["汽车电子", "RTOS", "C"], "https://campus.nio.com/", "蔚来招聘", 100),
    ("嵌入式软件工程师（自动驾驶）", "文远知行", "广州", "待确认", "嵌入式研发", 66, 0, 0, ["C++", "Linux", "自动驾驶"], "https://app.mokahr.com/campus_apply/jingchi/2137", "文远知行招聘", 100),
    ("嵌入式软件工程师", "小马智行", "北京", "待确认", "嵌入式研发", 65, 0, 0, ["自动驾驶", "Linux", "C++"], "https://campus.pony.ai/", "小马智行招聘", 100),
    ("嵌入式软件工程师", "Momenta", "北京", "待确认", "嵌入式研发", 64, 0, 0, ["自动驾驶", "C++", "Linux"], "https://momenta.jobs.feishu.cn/campus", "Momenta招聘", 100),
    ("嵌入式软件工程师", "OPPO", "深圳", "待确认", "嵌入式研发", 62, 0, 0, ["Linux", "驱动", "C"], "https://careers.oppo.com/university/oppo/campus", "OPPO招聘", 100),
    ("嵌入式软件工程师", "vivo", "东莞", "待确认", "嵌入式研发", 62, 0, 0, ["RTOS", "驱动", "C++"], "https://hr-campus.vivo.com/", "vivo招聘", 100),
    ("嵌入式软件工程师", "荣耀", "深圳", "待确认", "嵌入式研发", 61, 0, 0, ["Linux", "Android底层", "C"], "https://www.honor.com/cn/career/", "荣耀招聘", 100),
    ("嵌入式软件工程师", "联想", "北京", "待确认", "嵌入式研发", 60, 0, 0, ["硬件", "固件", "C"], "https://talent.lenovo.com.cn/position?projectType=3", "联想招聘", 100),
    ("嵌入式软件工程师", "传音控股", "深圳", "待确认", "嵌入式研发", 60, 0, 0, ["驱动", "C", "Android"], "https://transsion.zhiye.com/Campus", "传音控股招聘", 100),
    ("嵌入式软件工程师", "安克创新", "深圳", "待确认", "嵌入式研发", 61, 0, 0, ["智能硬件", "BLE", "RTOS"], "https://www.anker.com/careers", "安克创新招聘", 100),
    ("嵌入式软件工程师", "移远通信", "上海", "待确认", "嵌入式研发", 64, 0, 0, ["通信模组", "Linux", "C"], "https://talent.quectel.com/campus", "移远通信招聘", 100),
    ("嵌入式软件工程师", "广和通", "深圳", "待确认", "嵌入式研发", 63, 0, 0, ["物联网", "Linux", "C"], "https://fibocom.zhiye.com/", "广和通招聘", 100),
    ("嵌入式软件工程师", "中控技术", "杭州", "待确认", "嵌入式研发", 63, 0, 0, ["工业控制", "RTOS", "C"], "https://app.mokahr.com/campus-recruitment/supcon/148189", "中控技术招聘", 100),
    ("机器人控制软件工程师", "埃斯顿", "南京", "待确认", "机器人研发", 65, 0, 0, ["运动控制", "ROS", "C++"], "https://estun1.zhiye.com/campus", "埃斯顿招聘", 100),
    ("机器人软件工程师", "拓斯达", "东莞", "待确认", "机器人研发", 64, 0, 0, ["运动控制", "嵌入式", "C++"], "https://www.topstarltd.com/lang-cn/recruitinglist/006002003.html", "拓斯达招聘", 100),
    ("嵌入式软件工程师", "雷赛智能", "深圳", "待确认", "嵌入式研发", 63, 0, 0, ["运动控制", "CAN", "C"], "https://www.leisai.com/", "雷赛智能招聘", 100),
    ("嵌入式软件工程师", "新华三", "杭州", "待确认", "嵌入式研发", 61, 0, 0, ["网络设备", "Linux", "C"], "https://www.h3c.com/cn/About_Us/Join_Us/", "新华三招聘", 100),
    ("嵌入式软件工程师", "锐捷网络", "福州", "待确认", "嵌入式研发", 61, 0, 0, ["网络设备", "Linux", "C"], "https://www.ruijie.com.cn/join", "锐捷网络招聘", 100),
    ("嵌入式软件工程师（AI部署）", "商汤科技", "北京", "待确认", "嵌入式研发", 62, 0, 0, ["Jetson", "Linux", "C++"], "https://hr.sensetime.com/", "商汤科技招聘", 100),
    ("嵌入式软件工程师（边缘计算）", "旷视科技", "北京", "待确认", "嵌入式研发", 62, 0, 0, ["Jetson", "Linux", "C++"], "https://app.mokahr.com/campus_apply/megviihr/38642", "旷视科技招聘", 100),
    ("嵌入式软件工程师", "云从科技", "广州", "待确认", "嵌入式研发", 61, 0, 0, ["AI", "边缘计算", "Linux"], "https://www.cloudwalk.com/join", "云从科技招聘", 100),
    ("AI应用开发工程师", "智谱AI", "北京", "待确认", "机器人研发", 55, 0, 0, ["Python", "大模型", "Linux"], "https://www.zhipuai.cn/join", "智谱AI招聘", 100),
    ("AI算法工程师", "月之暗面", "北京", "待确认", "机器人研发", 55, 0, 0, ["Python", "大模型", "C++"], "https://www.moonshot.cn/join", "月之暗面招聘", 100),
    ("AI算法工程师", "MiniMax", "上海", "待确认", "机器人研发", 54, 0, 0, ["Python", "大模型", "Linux"], "https://www.minimax.io/careers", "MiniMax招聘", 100),
    ("嵌入式软件工程师（BMS）", "宁德时代", "宁德", "待确认", "嵌入式研发", 64, 0, 0, ["电池管理", "CAN", "C"], "https://talent.catl.com/", "宁德时代招聘", 100),
    ("嵌入式软件工程师（BMS）", "亿纬锂能", "惠州", "待确认", "嵌入式研发", 62, 0, 0, ["电池管理", "CAN", "C"], "https://www.evebattery.com/join", "亿纬锂能招聘", 100),
    ("嵌入式软件工程师（BMS）", "欣旺达", "深圳", "待确认", "嵌入式研发", 62, 0, 0, ["电池管理", "RTOS", "C"], "https://www.sunwoda.com/join", "欣旺达招聘", 100),
    ("嵌入式软件工程师", "美的", "佛山", "待确认", "嵌入式研发", 60, 0, 0, ["家电", "RTOS", "C"], "https://careers.midea.com/", "美的招聘", 100),
    ("嵌入式软件工程师", "格力", "珠海", "待确认", "嵌入式研发", 59, 0, 0, ["家电", "RTOS", "C"], "https://www.gree.com.cn/join", "格力招聘", 100),
    ("嵌入式软件工程师", "海尔", "青岛", "待确认", "嵌入式研发", 59, 0, 0, ["智能家居", "RTOS", "C"], "https://maker.haier.net/smart_home", "海尔招聘", 100),
    ("嵌入式软件工程师", "TCL", "深圳", "待确认", "嵌入式研发", 60, 0, 0, ["智能硬件", "Linux", "C"], "https://www.tcl.com/careers", "TCL招聘", 100),
    ("嵌入式软件工程师", "华星光电", "深圳", "待确认", "嵌入式研发", 59, 0, 0, ["显示驱动", "Linux", "C"], "https://www.szcsot.com/join", "华星光电招聘", 100),
]


def db_connect(autocommit: bool = False) -> sqlite3.Connection:
    if autocommit:
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def fingerprint(title: str, company: str, city: str, url: str) -> str:
    value = "|".join((title.strip().lower(), company.strip().lower(), city.strip().lower(), url.strip().lower()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def create_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        city TEXT NOT NULL DEFAULT '',
        salary TEXT NOT NULL DEFAULT '待确认',
        direction TEXT NOT NULL DEFAULT '嵌入式研发',
        match INTEGER NOT NULL DEFAULT 0,
        min_salary INTEGER NOT NULL DEFAULT 0,
        max_salary INTEGER NOT NULL DEFAULT 0,
        tags TEXT NOT NULL DEFAULT '[]',
        url TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT,
        first_seen INTEGER,
        last_seen INTEGER,
        active INTEGER NOT NULL DEFAULT 1,
        updated_at INTEGER,
        audience TEXT NOT NULL DEFAULT 'general'
    )""")


def create_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS tracking (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")


def has_unique_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return whether an old SQLite table has a single-column UNIQUE index."""
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not index[2]:
            continue
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()]
        if columns == [column]:
            return True
    return False


def migrate_jobs_table(conn: sqlite3.Connection) -> None:
    """Remove the preview schema's accidental UNIQUE(updated_at) constraint."""
    if not has_unique_column(conn, "jobs", "updated_at"):
        return
    # SQLite cannot drop an autoindex created by a column-level UNIQUE clause,
    # so copy the rows into the current schema. Existing user data is retained.
    conn.execute("DROP INDEX IF EXISTS idx_jobs_active_match")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_fingerprint")
    conn.execute("ALTER TABLE jobs RENAME TO jobs_preview_legacy")
    create_jobs_table(conn)
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs_preview_legacy)")}
    columns = [
        "id", "title", "company", "city", "salary", "direction", "match", "min_salary",
        "max_salary", "tags", "url", "source", "priority", "fingerprint", "first_seen",
        "last_seen", "active", "updated_at", "audience",
    ]
    columns = [column for column in columns if column in old_columns]
    names = ",".join(columns)
    conn.execute(f"INSERT INTO jobs ({names}) SELECT {names} FROM jobs_preview_legacy")
    conn.execute("DROP TABLE jobs_preview_legacy")


def is_relevant_job(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    if len(normalized) < 5 or any(term.lower() in normalized for term in NOISE_TERMS + MARKETING_TERMS + EXCLUDE_TERMS):
        return False
    return any(term.lower() in normalized for term in ROLE_TERMS) and any(term.lower() in normalized for term in JOB_TITLE_TERMS)


def is_source_job_link(source: dict, label: str, absolute: str) -> bool:
    """Filter navigation/product links before they enter the job cache."""
    if not is_relevant_job(label):
        return False
    parsed = urllib.parse.urlsplit(absolute)
    source_base = urllib.parse.urlsplit(source["url"])
    if parsed.netloc.lower() == source_base.netloc.lower() and parsed.path.rstrip("/") == source_base.path.rstrip("/") and not parsed.query:
        return False
    path_text = urllib.parse.unquote(f"{parsed.path}?{parsed.query}").lower()
    hints = JOB_PATH_HINTS.get(source["id"], DEFAULT_JOB_PATH_HINTS)
    # Aggregator and product-company homepages need an explicit recruitment
    # path. Official career portals are allowed to use a role-labelled detail
    # link even when their routing is opaque.
    if not any(hint.lower() in path_text for hint in hints):
        base_path = source_base.path.lower()
        same_portal = parsed.netloc.lower() == source_base.netloc.lower() and base_path and any(hint.lower() in base_path for hint in hints)
        if not same_portal:
            return False
    return True


def deactivate_irrelevant_jobs(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id,title,active FROM jobs").fetchall()
    for row in rows:
        if not is_relevant_job(row[1]) and row[2]:
            conn.execute("UPDATE jobs SET active=0 WHERE id=?", (row[0],))


def deactivate_irrelevant_directions(conn: sqlite3.Connection) -> None:
    """Hide pure-software / AI rows that are outside the embedded-robotics focus."""
    if not IRRELEVANT_DIRECTIONS:
        return
    rows = conn.execute("SELECT id,direction,active FROM jobs").fetchall()
    for row in rows:
        if row[1] in IRRELEVANT_DIRECTIONS and row[2]:
            conn.execute("UPDATE jobs SET active=0 WHERE id=?", (row[0],))


def normalize_cached_titles(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id,title,company,city,url,fingerprint FROM jobs WHERE active=1").fetchall():
        title = clean_job_title(row[1])
        if title == row[1]:
            continue
        new_fp = fingerprint(title, row[2], row[3], row[4])
        owner = conn.execute("SELECT id FROM jobs WHERE fingerprint=? AND id<>?", (new_fp, row[0])).fetchone()
        if owner:
            conn.execute("UPDATE jobs SET active=0 WHERE id=?", (row[0],))
        else:
            conn.execute("UPDATE jobs SET title=?,fingerprint=? WHERE id=?", (title, new_fp, row[0]))


def init_db() -> None:
    conn = db_connect()
    create_jobs_table(conn)
    migrate_jobs_table(conn)
    create_tracking_table(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        url TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        last_checked INTEGER,
        last_success INTEGER,
        last_error TEXT NOT NULL DEFAULT '',
        jobs_found INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        payload TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    migrations = {
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "fingerprint": "TEXT",
        "first_seen": "INTEGER",
        "last_seen": "INTEGER",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "updated_at": "INTEGER",
        "audience": "TEXT NOT NULL DEFAULT 'general'",
        "post_id": "TEXT",
    }
    for name, definition in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active_match ON jobs(active, match DESC)")
    registered_sources = all_sources() + jobpro_sources()
    for source in registered_sources:
        conn.execute("""INSERT INTO sources(id,name,kind,url,priority)
            VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,url=excluded.url,priority=excluded.priority,enabled=1""",
            (source["id"], source["name"], source["kind"], source["url"], source["priority"]))
    source_ids = tuple(source["id"] for source in registered_sources)
    placeholders = ",".join("?" for _ in source_ids)
    conn.execute(f"UPDATE sources SET enabled=0 WHERE id NOT IN ({placeholders})", source_ids)
    if conn.execute("SELECT 1 FROM profile WHERE id=1").fetchone() is None:
        default_profile = load_profile_file() or PROFILE
        conn.execute("INSERT INTO profile(id,payload,updated_at) VALUES(1,?,?)", (json.dumps(default_profile, ensure_ascii=False), int(time.time())))
    conn.commit()
    # Backfill fingerprints on databases created by the preview version.
    rows = conn.execute("SELECT id,title,company,city,url FROM jobs WHERE fingerprint IS NULL OR fingerprint='' ").fetchall()
    for row in rows:
        candidate = fingerprint(row[1], row[2], row[3], row[4])
        if conn.execute("SELECT 1 FROM jobs WHERE fingerprint=? AND id<>?", (candidate, row[0])).fetchone():
            candidate = f"{candidate}-{row[0]}"
        now = int(time.time())
        conn.execute("UPDATE jobs SET fingerprint=?,first_seen=COALESCE(first_seen,?),last_seen=COALESCE(last_seen,?),active=COALESCE(active,1) WHERE id=?", (candidate, now, now, row[0]))
    # Duplicate fingerprints can exist in preview databases. Keep the first
    # row visible and make later duplicates inactive before adding the index.
    duplicates = conn.execute("SELECT fingerprint, GROUP_CONCAT(id) FROM jobs WHERE fingerprint IS NOT NULL GROUP BY fingerprint HAVING COUNT(*) > 1").fetchall()
    for duplicate in duplicates:
        ids = [int(value) for value in str(duplicate[1]).split(",")]
        for duplicate_id in ids[1:]:
            conn.execute("UPDATE jobs SET active=0,fingerprint=? WHERE id=?", (f"{duplicate[0]}-{duplicate_id}", duplicate_id))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint)")
    deactivate_irrelevant_jobs(conn)
    deactivate_irrelevant_directions(conn)
    normalize_cached_titles(conn)
    conn.commit()
    conn.close()
    seed_jobs()


def seed_jobs() -> None:
    conn = db_connect()
    now = int(time.time() * 1000)
    for index, (title, company, city, salary, direction, score, low, high, tags, url, source, priority) in enumerate(SEED_JOBS):
        direction = infer_direction(f"{title} {' '.join(tags)}")
        if direction in IRRELEVANT_DIRECTIONS:
            continue
        fp = fingerprint(title, company, city, url)
        row = conn.execute("SELECT id FROM jobs WHERE fingerprint=?", (fp,)).fetchone()
        if row is None:
            # Migrate preview rows whose official URL was corrected.
            row = conn.execute("SELECT id FROM jobs WHERE title=? AND company=? LIMIT 1", (title, company)).fetchone()
            if row:
                owner = conn.execute("SELECT id FROM jobs WHERE fingerprint=? AND id<>?", (fp, row[0])).fetchone()
                if owner:
                    row = None
                else:
                    conn.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (fp, row[0]))
        row_time = now + index
        audience = "campus" if source in CAMPUS_SOURCE_NAMES else "general"
        values = (title, company, city, salary, direction, score, low, high, json.dumps(tags, ensure_ascii=False), url, source, priority, fp, row_time, row_time, 1, row_time, audience)
        if row:
            conn.execute("""UPDATE jobs SET title=?,company=?,city=?,salary=?,direction=?,match=?,min_salary=?,max_salary=?,tags=?,url=?,source=?,priority=?,last_seen=?,active=1,updated_at=?,audience=? WHERE id=?""", (*values[:12], row_time, row_time, values[17], row[0]))
        else:
            conn.execute("""INSERT INTO jobs(title,company,city,salary,direction,match,min_salary,max_salary,tags,url,source,priority,fingerprint,first_seen,last_seen,active,updated_at,audience)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
    conn.commit()
    conn.close()


def current_profile() -> dict:
    conn = db_connect()
    row = conn.execute("SELECT payload FROM profile WHERE id=1").fetchone()
    conn.close()
    if not row:
        return PROFILE.copy()
    try:
        payload = json.loads(row[0])
        return normalize_profile(payload, PROFILE)
    except (TypeError, json.JSONDecodeError):
        return PROFILE.copy()
    except ValueError:
        return PROFILE.copy()


def jobs_json() -> list[dict]:
    conn = db_connect()
    rows = conn.execute("""
        SELECT j.id AS id, j.title AS title, j.company AS company, j.city AS city,
               j.salary AS salary, j.direction AS direction, j.match AS match,
               j.min_salary AS min_salary, j.max_salary AS max_salary, j.tags AS tags,
               j.url AS url, j.source AS source, j.priority AS priority,
               j.last_seen AS last_seen, j.audience AS audience, j.first_seen AS first_seen,
               j.post_id AS post_id,
               t.status AS tracking_status, t.note AS tracking_note, t.updated_at AS tracking_updated
        FROM jobs j LEFT JOIN tracking t ON t.job_id = j.id
        WHERE j.active = 1
        ORDER BY j.match DESC, j.priority DESC, j.last_seen DESC
    """).fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
            if not isinstance(tags, list):
                tags = []
        except (TypeError, json.JSONDecodeError):
            tags = []
        tracking = None
        if row["tracking_status"]:
            tracking = {
                "status": row["tracking_status"],
                "note": row["tracking_note"] or "",
                "updatedAt": row["tracking_updated"],
            }
        result.append({
            "id": row["id"], "title": row["title"], "company": row["company"],
            "city": row["city"], "salary": row["salary"], "direction": row["direction"],
            "match": row["match"], "minSalary": row["min_salary"], "maxSalary": row["max_salary"],
            "tags": tags, "url": row["url"], "source": row["source"], "priority": row["priority"],
            "lastSeen": row["last_seen"], "audience": row["audience"], "firstSeen": row["first_seen"],
            "postId": row["post_id"],
            "tracking": tracking,
        })
    return result


def tracking_json() -> list[dict]:
    conn = db_connect()
    rows = conn.execute("""
        SELECT t.job_id AS job_id, t.status AS status, t.note AS note,
               t.created_at AS created_at, t.updated_at AS updated_at,
               j.title AS title, j.company AS company, j.city AS city, j.url AS url,
               j.match AS match, j.direction AS direction, j.audience AS audience,
               j.source AS source, j.active AS active
        FROM tracking t JOIN jobs j ON j.id = t.job_id
        ORDER BY t.updated_at DESC, j.match DESC
    """).fetchall()
    conn.close()
    return [{
        "jobId": row["job_id"], "status": row["status"], "note": row["note"] or "",
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "title": row["title"], "company": row["company"], "city": row["city"] or "",
        "url": row["url"], "match": row["match"], "direction": row["direction"],
        "audience": row["audience"], "source": row["source"], "active": bool(row["active"]),
    } for row in rows]


def save_tracking(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    try:
        job_id = int(payload.get("jobId", payload.get("job_id", 0)))
    except (TypeError, ValueError):
        raise ValueError("jobId 无效") from None
    if job_id <= 0:
        raise ValueError("jobId 无效")
    status = str(payload.get("status") or "").strip()
    note = str(payload.get("note") or "").strip()[:500]
    conn = db_connect()
    try:
        if conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
            raise ValueError("岗位不存在")
        now = int(time.time())
        if status in ("", "none"):
            conn.execute("DELETE FROM tracking WHERE job_id=?", (job_id,))
            conn.commit()
            return {"jobId": job_id, "status": "", "note": "", "updatedAt": now}
        if status not in TRACKING_STATUSES:
            raise ValueError("status 无效")
        conn.execute("""
            INSERT INTO tracking(job_id, status, note, created_at, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, (job_id, status, note, now, now))
        conn.commit()
        return {"jobId": job_id, "status": status, "note": note, "updatedAt": now}
    finally:
        conn.close()


def _jobpro_key_for_company(company: str) -> str | None:
    company = str(company or "").strip()
    for source in jobpro_sources():
        if source.get("company") == company:
            return source.get("key")
    return None


def _jobpro_cli(command: list[str], submit: bool = False, timeout: int = 120) -> dict:
    if not JOBPRO_CLI_PATH.is_file():
        raise RuntimeError(f"找不到 job-pro 投递 CLI：{JOBPRO_CLI_PATH}")
    env = os.environ.copy()
    if submit:
        env["JOB_PRO_I_UNDERSTAND_REAL_SUBMIT"] = "yes"
    completed = subprocess.run(
        ["node", str(JOBPRO_CLI_PATH), *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "job-pro 请求失败").strip()
        raise RuntimeError(detail[-2000:])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError((completed.stdout or completed.stderr or "job-pro 输出无法解析").strip()[-2000:]) from None
    return payload if isinstance(payload, dict) else {}


def _extract_post_id_from_url(url: str) -> str | None:
    value = url or ""
    match = re.search(r"(?:postId|postid)=([A-Za-z0-9_-]+)", value, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/(?:position|post|job|detail)/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    return None


def _normalize_apply_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _resolve_jobpro_post_id(company_key: str, job_row: sqlite3.Row) -> str:
    existing = str(job_row["post_id"] or "").strip()
    if existing:
        return existing
    derived = _extract_post_id_from_url(str(job_row["url"] or ""))
    if derived:
        return derived
    payload = _jobpro_cli([company_key, "all", "--compact"], timeout=90)
    positions = payload.get("positions") if payload.get("ok") else []
    target = _normalize_apply_url(str(job_row["url"] or ""))
    if isinstance(positions, list):
        for position in positions:
            if isinstance(position, dict) and _normalize_apply_url(str(position.get("apply_url") or "")) == target:
                return str(position.get("post_id") or "")
    return ""


def jobpro_apply(job_id: int, submit: bool) -> dict:
    conn = db_connect()
    row = conn.execute("SELECT id,title,company,url,audience,post_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError("岗位不存在")
    payload = {
        "jobId": job_id, "title": row["title"], "company": row["company"],
        "url": row["url"], "supported": False, "submit": submit,
        "cliPath": str(JOBPRO_CLI_PATH),
    }
    key = _jobpro_key_for_company(row["company"])
    if not key:
        payload["message"] = f"{row['company']} 不在 job-pro 50 家官方投递范围内，请手动打开投递页。"
        return payload
    payload["companyKey"] = key
    if key in JOBPRO_EXTERNAL_ONLY:
        payload["message"] = "该公司走微信/猎聘渠道，没有自动提交接口，job-pro 只能打开投递页，请手动投递。"
        return payload
    if str(row["audience"] or "") != "campus":
        payload["message"] = "自动投递只开放给校招岗位，社招请手动投递。"
        return payload
    post_id = _resolve_jobpro_post_id(key, row)
    if not post_id:
        raise RuntimeError("无法解析该岗位的 job-pro post_id，请先在官方投递页手动投递一次，再回来用一键投递")
    command = [key, "apply", post_id, "--compact"]
    if submit:
        command.append("--really-submit")
    result = _jobpro_cli(command, submit=submit)
    ok = result.get("ok") in (True, "true") or any(field in result for field in ("submitted", "application_submitted", "applicationId"))
    if submit and ok:
        save_tracking({"jobId": job_id, "status": "applied", "note": "一键自动投递"})
    payload.update({"supported": True, "ok": bool(ok), "postId": post_id, "result": result})
    message = str(result.get("message") or result.get("hint") or "").strip()
    if message:
        payload["message"] = message
    return payload


def jobpro_profile_status() -> dict:
    result: dict = {"path": str(JOBPRO_PROFILE_PATH), "exists": False, "missing": [], "hasResume": False, "profile": None}
    if not JOBPRO_PROFILE_PATH.is_file():
        result["missing"] = ["first_name", "last_name", "email", "phone"]
        return result
    try:
        data = json.loads(JOBPRO_PROFILE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        result["missing"] = ["first_name", "last_name", "email", "phone", "(配置文件损坏)"]
        return result
    if not isinstance(data, dict):
        result["missing"] = ["first_name", "last_name", "email", "phone", "(配置文件损坏)"]
        return result
    result["exists"] = True
    result["missing"] = [key for key in ("first_name", "last_name", "email", "phone") if not str(data.get(key) or "").strip()]
    resume_path = str(data.get("resume_path") or "").strip()
    result["hasResume"] = bool(resume_path) and Path(resume_path).is_file()
    result["profile"] = {
        "first_name": str(data.get("first_name") or "").strip(),
        "last_name": str(data.get("last_name") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "phone": str(data.get("phone") or "").strip(),
        "degree": str(data.get("degree") or "").strip(),
        "graduation_year": data.get("graduation_year"),
        "resume_path": resume_path,
        "custom": data.get("custom") if isinstance(data.get("custom"), dict) else {},
    }
    return result


def _normalize_jobpro_profile(profile: object) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("profile 必须是 JSON 对象")
    result = {
        "first_name": str(profile.get("first_name") or "").strip()[:80],
        "last_name": str(profile.get("last_name") or "").strip()[:80],
        "email": str(profile.get("email") or "").strip()[:200],
        "phone": str(profile.get("phone") or "").strip()[:60],
        "resume_path": str(profile.get("resume_path") or "").strip(),
    }
    degree = str(profile.get("degree") or "").strip().lower()
    if degree:
        if degree not in ("bachelor", "master", "phd"):
            raise ValueError("degree 只能是 bachelor / master / phd")
        result["degree"] = degree
    graduation = profile.get("graduation_year")
    if graduation not in (None, ""):
        try:
            result["graduation_year"] = int(graduation)
        except (TypeError, ValueError):
            raise ValueError("graduation_year 必须是年份数字") from None
    cover = str(profile.get("cover_letter_text") or "").strip()
    if cover:
        result["cover_letter_text"] = cover[:20000]
    custom = profile.get("custom")
    if isinstance(custom, dict):
        result["custom"] = {str(key).strip()[:100]: str(value).strip()[:500] for key, value in custom.items() if str(value).strip()}
    return result


def save_jobpro_profile(profile: object) -> dict:
    normalized = _normalize_jobpro_profile(profile)
    missing = [key for key in ("first_name", "last_name", "email", "phone") if not normalized.get(key)]
    if missing:
        raise ValueError("还缺必填项：" + "、".join(missing))
    if not normalized.get("resume_path") or not Path(normalized["resume_path"]).is_file():
        raise ValueError("还缺 resume_path：请先上传简历，或确认简历文件路径存在")
    JOBPRO_DIR.mkdir(parents=True, exist_ok=True)
    JOBPRO_PROFILE_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(JOBPRO_PROFILE_PATH), "profile": normalized}


def parse_resume_for_jobpro(attachments: list | None) -> dict:
    if not isinstance(attachments, list) or not attachments:
        raise ValueError("请先上传简历（PDF / Word / 图片）")
    JOBPRO_DIR.mkdir(parents=True, exist_ok=True)
    resume_dir = JOBPRO_DIR / f"resume_{int(time.time())}"
    resume_dir.mkdir(parents=True, exist_ok=True)
    note, _images = extract_attachment(attachments[0], resume_dir)
    files = [path for path in resume_dir.iterdir() if path.is_file()]
    resume_path = str(files[0]) if files else ""
    if not resume_path:
        raise RuntimeError("未能保存简历文件")
    prompt = (
        "你是简历信息提取助手。请解析附件中的简历，只输出一个 JSON 对象，不要输出任何解释、不要用 Markdown 代码块。\n"
        '格式：{"first_name":"名","last_name":"姓（中文名可留空）","email":"邮箱","phone":"电话",'
        '"degree":"bachelor 或 master 或 phd（不确定就填空字符串）","graduation_year":毕业年份数字,'
        '"custom":{"linkedin_url":"如有","nationality":"国籍/中国"}}\n'
        "要求：中文姓名把全名放入 first_name，last_name 留空；找不到的字段填空；毕业年份填预计毕业年份。"
    )
    answer = run_codex([{"role": "user", "content": prompt}], attachments)
    match = re.search(r"\{[\s\S]*\}", answer or "")
    if not match:
        raise RuntimeError("AI 没有返回 JSON，请重试或手动填写")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI 返回的 JSON 无法解析：{exc}") from None
    profile = _normalize_jobpro_profile(parsed)
    profile["resume_path"] = resume_path
    missing = [key for key in ("first_name", "last_name", "email", "phone") if not profile.get(key)]
    return {"ok": True, "profile": profile, "missing": missing, "resumePath": resume_path, "note": note[:200]}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href = ""
        self.text: list[str] = []
        self.anchor_attrs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.anchor_attrs = dict(attrs)
            self.href = self.anchor_attrs.get("href", "")
            self.text = []

    def handle_data(self, data):
        if self.href:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href:
            label = re.sub(r"\s+", " ", html.unescape(" ".join(self.text))).strip()
            label = label or self.anchor_attrs.get("aria-label", "") or self.anchor_attrs.get("title", "")
            self.links.append((label, self.href))
            self.href, self.text, self.anchor_attrs = "", [], {}


def fetch_public_page(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "QiuzhaoRadar/1.0 (personal job reader)", "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(2 * 1024 * 1024)
        charset = response.headers.get_content_charset()
        if not charset:
            probe = data[:8192].decode("ascii", errors="ignore")
            match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", probe, re.IGNORECASE)
            charset = match.group(1) if match else "utf-8"
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")


def _http_json_post(url: str, payload: dict, headers: dict[str, str], timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(6 * 1024 * 1024)
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def firecrawl_scrape(url: str) -> tuple[str, str]:
    """Layer 1: render the page with Firecrawl and return (html, markdown)."""
    if not FIRECRAWL_API_KEY:
        raise RuntimeError("Firecrawl 未配置")
    result = _http_json_post(
        "https://api.firecrawl.dev/v1/scrape",
        {"url": url, "formats": ["html", "markdown"], "onlyMainContent": False, "timeout": 30000},
        {"Content-Type": "application/json", "Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
        timeout=60,
    )
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or "Firecrawl 请求失败"))
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    html = str(data.get("html") or "").strip()
    markdown = str(data.get("markdown") or "").strip()
    if not html and not markdown:
        raise RuntimeError("Firecrawl 返回空内容")
    return html, markdown


def anysearch_extract(url: str) -> str:
    """Layer 2: anonymous/cloud AnySearch extract, returns markdown text."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Anysearch-Client": "skill/3.0.1",
    }
    if ANYSEARCH_API_KEY:
        headers["Authorization"] = f"Bearer {ANYSEARCH_API_KEY}"
    result = _http_json_post(
        "https://api.anysearch.com/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "extract", "arguments": {"url": url}}},
        headers,
        timeout=25,
    )
    if result.get("error"):
        error = result["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(f"AnySearch 错误：{message}")
    content = ((result.get("result") or {}).get("content") or []) if isinstance(result.get("result"), dict) else []
    markdown = "\n".join(
        str(item.get("text") or "") for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()
    if not markdown:
        raise RuntimeError("AnySearch 返回空内容")
    return markdown


def fetch_rendered_content(url: str) -> tuple[str | None, str | None]:
    """Run the layered renderer for expanded sources.

    Returns (html, markdown); either may be empty. Returns (None, None) when no
    render layer is configured/available and the caller should use its plain
    HTTP fetch instead.
    """
    if FIRECRAWL_API_KEY:
        try:
            html, markdown = firecrawl_scrape(url)
            return html or None, markdown or None
        except Exception:
            pass
    if ANYSEARCH_ENABLED:
        try:
            return None, anysearch_extract(url)
        except Exception:
            return None, None
    return None, None


def infer_direction(text: str) -> str:
    value = text.lower()
    if any(word in value for word in ("机器人", "ros", "jetson", "slam", "运动控制", "具身智能", "四足", "人形")):
        return "机器人研发"
    if any(word in value for word in ("嵌入式", "固件", "驱动", "单片机", "mcu", "rtos", "bsp", "底层")):
        return "嵌入式研发"
    if any(word in value for word in ("自动驾驶", "智能驾驶", "无人驾驶", "整车", "汽车电子", "bms", "电池管理", "电控", "车载")):
        return "汽车电子"
    if any(word in value for word in ("算法", "机器学习", "深度学习", "大模型", "nlp", "计算机视觉", "人工智能", "强化学习", "感知", "规划", " ai", "ai ")):
        return "算法与AI"
    if any(word in value for word in ("硬件", "fpga", "pcb", "模拟", "电源", "射频", "ic", "芯片")):
        return "硬件研发"
    if any(word in value for word in ("测试", "质量", "qa")):
        return "测试开发"
    if any(word in value for word in ("后端", "服务端", "java", "go", "golang", "微服务", "web", "全栈", "数据库", "云计算", "devops", "运维")):
        return "后端开发"
    if any(word in value for word in ("通信", "5g", "基带", "网络协议", "光通信")):
        return "通信研发"
    return "软件研发"


def infer_city(text: str) -> str:
    for city in ("北京", "上海", "杭州", "深圳", "成都", "广州", "南京", "武汉", "西安"):
        if city in text:
            return city
    return ""


def infer_company(source: dict, text: str) -> str:
    if source.get("company"):
        return str(source["company"])
    known = ("大疆", "宇树科技", "海康威视", "小米", "汇川技术", "科大讯飞", "华为", "比亚迪", "中兴通讯", "阿里巴巴", "百度", "字节跳动", "腾讯", "美团")
    for company in known:
        if company.lower() in text.lower():
            return company
    return source["name"]


def clean_job_title(label: str) -> str:
    """Keep the position name while dropping inline duties/portal metadata."""
    value = re.sub(r"\s+", " ", str(label or "")).strip()
    value = re.split(r"\s*(?:热招|急招|岗位职责|职位描述|工作内容|任职资格|招聘详情)\s*", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"\s*[|｜]\s*", value, maxsplit=1)[0]
    value = re.sub(r"\s*[（(][A-Za-z0-9]+[）)]", "", value)
    value = value.strip(" -—:：")
    return value[:120] or str(label or "").strip()[:120]


def infer_salary(text: str) -> tuple[str, int, int]:
    """Extract a simple K/month range when a public title exposes one."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*[Kk千]\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*[Kk千]?", text)
    if not match:
        return "待确认", 0, 0
    low, high = float(match.group(1)), float(match.group(2))
    if high < low:
        low, high = high, low
    low_int, high_int = round(low), round(high)
    return f"{low_int}-{high_int}K", low_int, high_int


def score_candidate(text: str, profile: dict) -> int:
    haystack = text.lower()
    terms = [*profile.get("skills", []), *profile.get("keywords", [])]
    hits = sum(1 for term in terms if term.lower() in haystack)
    return min(95, 55 + hits * 5)


def normalize_profile(payload: object, base: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("profile 必须是 JSON 对象")
    source = dict(base or PROFILE)
    source.update(payload)
    normalized: dict[str, object] = {
        "version": str(source.get("version") or PROFILE["version"])[:40],
        "major": str(source.get("major") or "")[:120],
    }
    list_fields = ("targetRoles", "skills", "keywords", "preferredCities")
    for field in list_fields:
        values = source.get(field, [])
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{field} 必须是数组")
        cleaned = []
        for value in values[:50]:
            item = str(value).strip()[:80]
            if item and item not in cleaned:
                cleaned.append(item)
        normalized[field] = cleaned
    # Optional structured fields are kept small for the future AI adapter.
    for field in ("summary", "projects", "experience"):
        if field in source:
            value = source[field]
            if isinstance(value, str):
                normalized[field] = value[:12000]
            elif isinstance(value, list):
                normalized[field] = [str(item)[:500] for item in value[:30]]
    if len(json.dumps(normalized, ensure_ascii=False)) > 64000:
        raise ValueError("profile 内容过大")
    return normalized


def save_profile(profile: dict) -> None:
    conn = db_connect()
    now = int(time.time())
    conn.execute(
        "INSERT INTO profile(id,payload,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
        (json.dumps(profile, ensure_ascii=False), now),
    )
    rows = conn.execute("SELECT id,title,company,city,tags,url FROM jobs WHERE active=1").fetchall()
    for row in rows:
        try:
            tags = json.loads(row[4] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        text = " ".join((str(row[1] or ""), str(row[2] or ""), str(row[3] or ""), " ".join(map(str, tags)), str(row[5] or "")))
        conn.execute("UPDATE jobs SET match=?,updated_at=? WHERE id=?", (score_candidate(text, profile), now * 1000 + int(row[0]), row[0]))
    conn.commit()
    conn.close()


def parse_profile_with_ai(attachments: list | None) -> dict:
    """Parse an uploaded resume into the app's structured resume profile."""
    if not isinstance(attachments, list) or not attachments:
        raise ValueError("请先上传简历（PDF / Word / 图片）")
    prompt = (
        "你是简历画像提取助手。请解析附件中的简历，只输出一个 JSON 对象，不要输出任何解释、不要用 Markdown 代码块：\n"
        '{"targetRoles":["嵌入式研发","机器人研发"],"major":"计算机科学与技术",'
        '"skills":["C/C++","Python","STM32"],"keywords":["机器人控制","运动控制"],'
        '"preferredCities":["深圳","杭州"],"summary":"一句话概括求职定位"}\n'
        "要求：targetRoles 从“嵌入式研发/机器人研发/硬件研发/汽车电子/算法与AI/测试开发/后端开发/软件研发”中选 1-3 个最匹配的；"
        "skills 只提取简历里真实出现的技能；preferredCities 从简历意向城市推断、最多 5 个；"
        "找不到的字段用空数组或空字符串。"
    )
    answer = run_codex([{"role": "user", "content": prompt}], attachments[:MAX_ATTACHMENTS])
    match = re.search(r"\{[\s\S]*\}", answer or "")
    if not match:
        raise RuntimeError("AI 没有返回 JSON，请重试或手动调整画像")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI 返回的画像 JSON 无法解析：{exc}") from None
    return normalize_profile(parsed, current_profile())


def source_relevance(source: dict, profile: dict) -> int:
    """Score a source against the active resume so refresh prioritizes the best-matching companies."""
    tags = [str(tag).lower() for tag in SOURCE_TAGS.get(source["id"], [])]
    extra_fields = (
        str(source.get("industry", "")),
        str(source.get("categories", "")),
        str(source.get("batch", "")),
        str(source.get("locations", "")),
    )
    text = " ".join((str(source.get("name", "")), str(source.get("company", "")), " ".join(tags), *extra_fields)).lower()
    core = set()
    for role in profile.get("targetRoles", []):
        word = str(role).replace("研发", "").strip().lower()
        if word:
            core.add(word)
    for keyword in profile.get("keywords", []):
        value = str(keyword).lower()
        for word in ("机器人", "嵌入式", "运动控制", "硬件"):
            if word in value:
                core.add(word)
    return sum(2 for word in core if word in text)


def extract_markdown_titles(markdown: str) -> list[str]:
    """Pull plausible job titles out of markdown returned by a render layer.

    Render layers such as Firecrawl/AnySearch often return extracted text
    without a one-to-one anchor list. These candidates are still fed through
    the project's own relevance filter before they become job cards.
    """
    seen: set[str] = set()
    titles: list[str] = []
    for raw_line in (markdown or "").splitlines():
        line = re.sub(r"^[\s>#*\-+\d.`]+", "", raw_line).strip()
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_~`]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 5:
            continue
        if re.search(r"https?://|:\s*邮箱|\d{6,}", line):
            continue
        if not is_relevant_job(line):
            continue
        if any(token in line for token in ("岗位职责", "职位描述", "任职要求", "工作内容", "福利待遇", "公司介绍", "招聘流程", "常见问题", "FAQ")):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(clean_job_title(line)[:120])
        if len(titles) >= 80:
            break
    return titles


def store_job(conn: sqlite3.Connection, source: dict, title: str, absolute: str, text: str, profile: dict, checked: int, found: int, audience: str | None = None) -> bool:
    """Insert/refresh one discovered job through the project's custom pipeline."""
    company = infer_company(source, text)
    city = infer_city(text)
    direction = infer_direction(text)
    if direction in IRRELEVANT_DIRECTIONS:
        return False
    title = clean_job_title(title)
    tags = [term for term in profile.get("skills", []) if term.lower() in text.lower()][:4]
    if not tags:
        tags = ["公开岗位"]
    fp = fingerprint(title, company, city, absolute)
    salary, min_salary, max_salary = infer_salary(text)
    row = conn.execute("SELECT id FROM jobs WHERE fingerprint=?", (fp,)).fetchone()
    if row is None:
        # A previous refresh may have stored the same URL with a noisy
        # title. Reuse that row when the title cleaner changes it.
        row = conn.execute("SELECT id,priority FROM jobs WHERE url=? ORDER BY id LIMIT 1", (absolute,)).fetchone()
    if row is None:
        # The same opening is often reposted by an aggregator with a
        # different URL. Prefer the higher-priority source and avoid
        # showing duplicate cards for an identical title/company/city.
        similar = conn.execute(
            "SELECT id,priority FROM jobs WHERE active=1 AND title=? AND company=? AND city=? ORDER BY priority DESC,id LIMIT 1",
            (title, company, city),
        ).fetchone()
        if similar:
            if int(similar[1] or 0) >= int(source["priority"]):
                return False
            row = similar
    if row is not None and conn.execute("SELECT 1 FROM jobs WHERE fingerprint=? AND id<>?", (fp, row[0])).fetchone():
        # A stale duplicate owns this fingerprint; retaining the
        # existing record is safer than violating the unique index.
        return False
    row_time = checked * 1000 + found
    if audience is None:
        audience = "campus" if source["id"] in CAMPUS_SOURCE_IDS or source.get("campus") else "general"
    values = (title, company, city, salary, direction, score_candidate(text, profile), min_salary, max_salary, json.dumps(tags, ensure_ascii=False), absolute, source["name"], source["priority"], fp, checked, checked, 1, row_time, audience)
    if row:
        conn.execute("UPDATE jobs SET title=?,company=?,city=?,salary=?,direction=?,match=?,min_salary=?,max_salary=?,tags=?,url=?,source=?,priority=?,fingerprint=?,last_seen=?,active=1,updated_at=?,audience=? WHERE id=?", (*values[:13], checked, row_time, values[17], row[0]))
    else:
        conn.execute("""INSERT INTO jobs(title,company,city,salary,direction,match,min_salary,max_salary,tags,url,source,priority,fingerprint,first_seen,last_seen,active,updated_at,audience)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
    return True


def refresh_source(source: dict) -> int:
    checked = int(time.time())
    conn = db_connect(autocommit=True)
    conn.execute("UPDATE sources SET last_checked=?,last_error='' WHERE id=?", (checked, source["id"]))
    found = 0
    try:
        profile = current_profile()
        html = None
        markdown = None
        if source.get("rendered"):
            html, markdown = fetch_rendered_content(source["url"])
            if not html and not markdown:
                html = fetch_public_page(source["url"])
        else:
            html = fetch_public_page(source["url"])
        parser = LinkParser()
        if html:
            parser.feed(html)
        seen_fingerprints: set[str] = set()
        for label, href in parser.links[:500]:
            absolute = urllib.parse.urljoin(source["url"], href)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                continue
            clean_query = urllib.parse.urlencode([(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if not key.lower().startswith(("utm_", "from", "source"))])
            # Hash routes are part of many campus portals' job detail URLs;
            # retain them while removing only tracking query parameters.
            absolute = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, parsed.fragment))
            text = f"{label} {absolute}"
            if not is_source_job_link(source, label, absolute):
                continue
            if absolute in seen_fingerprints:
                continue
            seen_fingerprints.add(absolute)
            if store_job(conn, source, label, absolute, text, profile, checked, found):
                found += 1
                if found >= 30:
                    break
        # Render layers often return extracted text with no anchor list. When
        # the HTML pass found nothing, accept clearly relevant lines as jobs
        # that point back at the company's portal.
        if found == 0 and markdown:
            context_text = " ".join(filter(None, (
                str(source.get("name", "")),
                str(source.get("company", "")),
                str(source.get("industry", "")),
                str(source.get("batch", "")),
            )))
            for title in extract_markdown_titles(markdown):
                text = f"{title} {context_text}"
                if store_job(conn, source, title, source["url"], text, profile, checked, found):
                    found += 1
                    if found >= 15:
                        break
        if found:
            conn.execute("UPDATE sources SET last_success=?,jobs_found=?,last_error='' WHERE id=?", (checked, found, source["id"]))
        else:
            conn.execute("UPDATE sources SET jobs_found=0,last_error=? WHERE id=?", (SOURCE_EMPTY_ERROR, source["id"]))
        conn.commit()
    except Exception as exc:
        conn.execute("UPDATE sources SET last_error=? WHERE id=?", (str(exc)[:500], source["id"]))
        conn.commit()
    finally:
        conn.close()
    return found


def refresh_extra_pool(profile: dict) -> int:
    """Round-robin crawl of the expanded xiaozhao pool, a small slice per run."""
    extras = all_sources()[len(SOURCES):]
    if not extras:
        return 0
    extras = sorted(extras, key=lambda source: source_relevance(source, profile), reverse=True)
    conn = db_connect(autocommit=True)
    row = conn.execute("SELECT value FROM meta WHERE key='extra_pool_cursor'").fetchone()
    conn.close()
    try:
        cursor = int(row[0]) if row and str(row[0]).isdigit() else 0
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0 or cursor >= len(extras):
        cursor = 0
    batch = extras[cursor:cursor + EXTRA_BATCH_SIZE]
    total = 0
    for source in batch:
        total += refresh_source(source)
    next_cursor = (cursor + EXTRA_BATCH_SIZE) % len(extras)
    now = int(time.time())
    conn = db_connect(autocommit=True)
    conn.execute("INSERT INTO meta(key,value) VALUES('extra_pool_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(next_cursor),))
    conn.execute("INSERT INTO meta(key,value) VALUES('extra_pool_size',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(len(extras)),))
    conn.execute("INSERT INTO meta(key,value) VALUES('extra_pool_checked',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    conn.commit()
    conn.close()
    return total


def _run_jobpro(key: str, timeout: int) -> dict:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise RuntimeError("本机没有找到 npx；job-pro 信源不可用")
    command = [npx, "--yes", "@ha7ch/job-pro@latest", key, "all", "--page-size", "100", "--compact"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "job-pro 请求失败").strip()
        raise RuntimeError(detail[-1200:])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError("job-pro 返回的不是有效 JSON") from None
    return payload if isinstance(payload, dict) else {}


def refresh_jobpro(profile: dict) -> int:
    """Round-robin official-API queries through the job-pro CLI.

    Returned positions are concrete job postings, so they skip HTML crawling
    entirely but still pass through the project's own relevance filter and
    profile scoring before being stored.
    """
    sources = jobpro_sources()
    if not sources:
        return 0
    sources = sorted(sources, key=lambda source: source_relevance(source, profile), reverse=True)
    conn = db_connect(autocommit=True)
    row = conn.execute("SELECT value FROM meta WHERE key='jobpro_cursor'").fetchone()
    conn.close()
    try:
        cursor = int(row[0]) if row and str(row[0]).isdigit() else 0
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0 or cursor >= len(sources):
        cursor = 0
    batch = sources[cursor:cursor + JOBPRO_BATCH_SIZE]
    total = 0
    for source in batch:
        checked = int(time.time())
        found = 0
        conn = db_connect(autocommit=True)
        conn.execute("""INSERT INTO sources(id,name,kind,url,priority,enabled)
            VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,url=excluded.url,priority=excluded.priority,enabled=1""",
            (source["id"], source["name"], source["kind"], source["url"], source["priority"]))
        conn.execute("UPDATE sources SET last_checked=?,last_error='' WHERE id=?", (checked, source["id"]))
        try:
            payload = _run_jobpro(source["key"], JOBPRO_TIMEOUT_SECONDS)
            positions = payload.get("positions") if payload.get("ok") else []
            if not isinstance(positions, list):
                positions = []
            accepted: list[tuple[int, str, str, str, str, str]] = []
            for position in positions:
                if not isinstance(position, dict):
                    continue
                title = str(position.get("title") or "").strip()
                if not is_relevant_job(title):
                    continue
                url = str(position.get("apply_url") or "").strip()
                if not url:
                    continue
                post_id = str(position.get("post_id") or "").strip()
                url_lower = url.lower()
                job_audience = "general" if ("social" in url_lower or "intern" in url_lower) else "campus"
                text = " ".join(filter(None, (
                    title,
                    source["company"],
                    str(position.get("work_cities") or ""),
                    str(position.get("project") or ""),
                    str(position.get("bgs") or ""),
                    str(position.get("recruit_label") or ""),
                )))
                accepted.append((score_candidate(text, profile), title, url, text, job_audience, post_id))
            accepted.sort(key=lambda item: item[0], reverse=True)
            per_company_cap = 100 if source["key"] in JOBPRO_HIGH_CAP_KEYS else 40
            for _, title, url, text, job_audience, post_id in accepted[:per_company_cap * 3]:
                if store_job(conn, source, title, url, text, profile, checked, found, job_audience):
                    found += 1
                    if post_id:
                        conn.execute(
                            "UPDATE jobs SET post_id=? WHERE url=? AND (post_id IS NULL OR post_id='')",
                            (post_id, url),
                        )
                    if found >= per_company_cap:
                        break
            if found:
                conn.execute("UPDATE sources SET last_success=?,jobs_found=?,last_error='' WHERE id=?", (checked, found, source["id"]))
            else:
                conn.execute("UPDATE sources SET jobs_found=0,last_error=? WHERE id=?", (SOURCE_EMPTY_ERROR, source["id"]))
            conn.commit()
        except Exception as exc:
            conn.execute("UPDATE sources SET last_error=? WHERE id=?", (str(exc)[:500], source["id"]))
            conn.commit()
        finally:
            conn.close()
        total += found
    next_cursor = (cursor + JOBPRO_BATCH_SIZE) % len(sources)
    now = int(time.time())
    conn = db_connect(autocommit=True)
    conn.execute("INSERT INTO meta(key,value) VALUES('jobpro_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(next_cursor),))
    conn.execute("INSERT INTO meta(key,value) VALUES('jobpro_size',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(len(sources)),))
    conn.execute("INSERT INTO meta(key,value) VALUES('jobpro_checked',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    conn.commit()
    conn.close()
    return total


def refresh_all() -> dict:
    with REFRESH_LOCK:
        total = 0
        profile = current_profile()
        ranked_sources = sorted(SOURCES, key=lambda source: source_relevance(source, profile), reverse=True)
        for source in ranked_sources:
            total += refresh_source(source)
        total += refresh_jobpro(profile)
        total += refresh_extra_pool(profile)
        conn = db_connect()
        now = int(time.time())
        cutoff = now - 45 * 24 * 60 * 60
        seed_fingerprints = {fingerprint(item[0], item[1], item[2], item[9]) for item in SEED_JOBS}
        for row in conn.execute("SELECT id,title,fingerprint,last_seen,active FROM jobs").fetchall():
            keep_seed = row[2] in seed_fingerprints
            stale = row[3] is None or row[3] < cutoff
            if row[4] and (not is_relevant_job(row[1]) or (stale and not keep_seed)):
                conn.execute("UPDATE jobs SET active=0 WHERE id=?", (row[0],))
        deactivate_irrelevant_directions(conn)
        conn.execute("INSERT INTO meta(key,value) VALUES('last_refresh',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        conn.commit()
        conn.close()
        return {"sources": len(all_sources()), "jobsFound": total, "completedAt": now}


def refresh_loop() -> None:
    # Run once in the background at startup, then every six hours. A failure in
    # one public source never removes the cached jobs from other sources.
    time.sleep(1)
    try:
        refresh_all()
    except Exception:
        # Keep the service alive even if a database or network error escapes a
        # source-level guard; cached jobs remain available to the browser.
        pass
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            refresh_all()
        except Exception:
            pass


def get_ai_config() -> dict:
    """Runtime AI config: DB overrides env; falls back to Codex/CCSwitch."""
    conn = db_connect()
    values = {}
    for row in conn.execute("SELECT key,value FROM meta WHERE key IN ('ai_base_url','ai_model','ai_api_key')").fetchall():
        values[row["key"]] = row["value"]
    conn.close()
    base_url = values.get("ai_base_url") or os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("RELAXY_BASE_URL") or "https://api.deepseek.com"
    model = values.get("ai_model") or os.environ.get("DEEPSEEK_MODEL") or os.environ.get("RELAXY_MODEL") or "deepseek-chat"
    api_key = values.get("ai_api_key") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("RELAXY_API_KEY") or ""
    return {"base_url": str(base_url).rstrip("/"), "model": str(model), "api_key": str(api_key).strip()}


def save_ai_config(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    base_url = str(payload.get("baseUrl") or payload.get("base_url") or "").strip().rstrip("/")
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("apiKey") or payload.get("api_key") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http(s):// 开头")
    if not model:
        raise ValueError("模型名不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")
    conn = db_connect()
    now = int(time.time())
    for key, value in (("ai_base_url", base_url), ("ai_model", model), ("ai_api_key", api_key)):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.execute("INSERT INTO meta(key,value) VALUES('ai_updated_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    conn.commit()
    conn.close()
    masked = api_key[:4] + "****" + api_key[-4:] if len(api_key) > 8 else "****"
    return {"ok": True, "baseUrl": base_url, "model": model, "apiKeyMasked": masked}


def _ocr_image_text(path: Path) -> str:
    """OCR one image via RapidOCR (open-source, local, Chinese+English)."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        raise RuntimeError("本机未安装 RapidOCR，无法识别图片；请运行 pip install rapidocr_onnxruntime") from None
    engine = RapidOCR()
    result, _ = engine(str(path))
    if not result:
        return ""
    return "\n".join(str(item[1]) for item in result if item and len(item) > 1)


def _pdf_ocr_text(path: Path) -> str:
    """Render a scanned PDF to images (PyMuPDF) and OCR each page."""
    try:
        import pymupdf
    except Exception:
        raise RuntimeError("本机未安装 PyMuPDF，无法 OCR 扫描版 PDF；请运行 pip install pymupdf") from None
    pages_text = []
    with pymupdf.open(str(path)) as document:
        for page in document:
            pix = page.get_pixmap(dpi=180)
            temp_image = path.with_suffix(f".page{page.number}.png")
            pix.save(str(temp_image))
            try:
                text = _ocr_image_text(temp_image)
            finally:
                try:
                    temp_image.unlink(missing_ok=True)
                except OSError:
                    pass
            if text:
                pages_text.append(text)
    return "\n".join(pages_text)


def _deepseek_chat(prompt: str, config: dict) -> str:
    """OpenAI-compatible chat call (DeepSeek et al.). Text-only."""
    url = f"{config['base_url']}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }
    body = json.dumps({
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read(6 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"AI 接口返回 {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AI 接口连接失败：{exc.reason}") from None
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise RuntimeError("AI 接口返回的不是 JSON") from None
    if not isinstance(parsed, dict) or parsed.get("error"):
        raise RuntimeError(str(parsed.get("error") or "AI 接口返回错误")[:1200])
    try:
        return parsed["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("AI 接口返回格式异常") from None


def codex_executable() -> str | None:
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return found
    candidates = list(Path.home().glob("AppData/Local/OpenAI/Codex/bin/*/codex.exe"))
    candidates += list(Path.home().glob("AppData/Local/OpenAI/Codex/bin/*/codex.EXE"))
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


def extract_attachment(attachment: dict, temp_dir: Path) -> tuple[str, list[str]]:
    if not isinstance(attachment, dict):
        raise ValueError("附件格式无效")
    name = str(attachment.get("name") or "attachment")[:120]
    mime = str(attachment.get("mime") or attachment.get("type") or "application/octet-stream").lower()
    data = str(attachment.get("data") or "")
    if not data:
        raise ValueError(f"附件 {name} 为空")
    if "," in data and data.startswith("data:"):
        data = data.split(",", 1)[1]
    data = re.sub(r"\s+", "", data)
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(f"附件 {name} 编码无效") from None
    if not raw:
        raise ValueError(f"附件 {name} 为空")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"附件 {name} 超过 8MB")
    path = temp_dir / re.sub(r"[^A-Za-z0-9._-]", "_", name)
    path.write_bytes(raw)
    lower = name.lower()
    if mime == "application/pdf" or lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            text = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages).strip()
            if len(text) >= 20:
                return f"附件 {name} 的文本内容：\n{text[:120000]}", []
            raise RuntimeError("PDF 没有文本层，转为 OCR")
        except Exception:
            try:
                ocr_text = _pdf_ocr_text(path)
                if ocr_text.strip():
                    return f"附件 {name} 的 OCR 文本内容：\n{ocr_text[:120000]}", []
            except Exception:
                pass
            return f"附件 {name} 已上传，但本机无法提取文本且 OCR 失败；请根据文件名提示用户。", []
    if lower.endswith(".docx") or mime.endswith("wordprocessingml.document"):
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", " ", xml)
            return f"附件 {name} 的文本内容：\n{html.unescape(text)[:120000]}", []
        except Exception:
            return f"附件 {name} 已上传，但本机无法提取文档文本。", []
    if mime.startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return f"请分析附件图片 {name}。", [str(path)]
    return f"附件 {name} 已上传。", []


def run_codex(messages: list[dict], attachments: list[dict] | None = None) -> str:
    if not isinstance(messages, list):
        raise ValueError("messages 必须是数组")
    if attachments is not None and not isinstance(attachments, list):
        raise ValueError("attachments 必须是数组")
    if len(attachments or []) > MAX_ATTACHMENTS:
        raise ValueError(f"最多同时上传 {MAX_ATTACHMENTS} 个附件")
    ai_config = get_ai_config()
    with tempfile.TemporaryDirectory(prefix="qiuzhao-ai-") as temp_name:
        temp_dir = Path(temp_name)
        attachment_notes, images = [], []
        for attachment in attachments or []:
            note, image_paths = extract_attachment(attachment, temp_dir)
            attachment_notes.append(note)
            images.extend(image_paths)
        profile = current_profile()
        transcript = [
            "你是一个只提供文字建议的秋招简历助手。不要执行命令、不要修改文件、不要泄露凭据。",
            "请基于以下求职画像，回答用户问题；需要时给出具体可执行的岗位筛选或简历修改建议。",
            "求职画像：" + json.dumps(profile, ensure_ascii=False),
        ]
        for message in messages[-MAX_MESSAGES:]:
            if not isinstance(message, dict):
                continue
            role = "用户" if message.get("role") == "user" else "助手"
            content = str(message.get("content", ""))[:20000]
            transcript.append(f"{role}：\n{content}")
        transcript.extend(attachment_notes)
        prompt = "\n\n".join(transcript)
        if ai_config.get("api_key"):
            # DeepSeek 文本接口不读图：图片附件先在本机 OCR 成文字。
            if images:
                ocr_parts = []
                for image in images:
                    try:
                        ocr_text = _ocr_image_text(Path(image))
                        ocr_parts.append(ocr_text if ocr_text else "[图片未识别出文字]")
                    except Exception as exc:
                        ocr_parts.append(f"[图片 OCR 失败：{exc}]")
                if ocr_parts:
                    prompt += "\n\n附件图片 OCR 文本：\n" + "\n".join(ocr_parts)
            return _deepseek_chat(prompt, ai_config)
        executable = codex_executable()
        if not executable:
            raise RuntimeError("本机没有找到 Codex 可执行文件，也没有配置 DeepSeek API Key")
        output_path = temp_dir / "answer.txt"
        command = [executable, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "-m", MODEL, "-o", str(output_path)]
        for image in images:
            command.extend(["--image", image])
        command.append("-")
        completed = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=180, cwd=str(ROOT), env=os.environ.copy())
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Codex 请求失败").strip()
            if any(token in detail.lower() for token in ("暂无可用通道", "out of our service region", "no available", "503")):
                raise UpstreamUnavailable(detail[-1200:])
            raise RuntimeError(detail[-1200:])
        answer = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
        if not answer:
            answer = (completed.stdout or "").strip().splitlines()[-1:] and (completed.stdout or "").strip().splitlines()[-1]
        return answer or "AI 没有返回内容。"


class UpstreamUnavailable(RuntimeError):
    """The configured provider is reachable but currently has no route."""


class RadarHandler(BaseHTTPRequestHandler):
    ALLOWED_STATIC = {"/", "/index.html", "/styles.css", "/app.js"}

    def send_json(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin", "")
        if origin in ("http://127.0.0.1:5500", "http://localhost:5500"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if status != 204:
            self.wfile.write(raw)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length 无效") from None
        if length < 0:
            raise ValueError("请求体长度无效")
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/chat":
            self.send_json(405, {"error": "GET 不支持；请使用 POST /api/chat"})
        elif path == "/api/jobs":
            self.send_json(200, jobs_json())
        elif path == "/api/tracking":
            self.send_json(200, tracking_json())
        elif path == "/api/ai/config":
            cfg = get_ai_config()
            key = cfg["api_key"]
            masked = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("****" if key else "")
            self.send_json(200, {
                "baseUrl": cfg["base_url"], "model": cfg["model"],
                "apiKeyMasked": masked, "configured": bool(key),
            })
        elif path == "/api/profile":
            self.send_json(200, current_profile())
        elif path == "/api/sources":
            conn = db_connect(); rows = conn.execute("SELECT id,name,kind,url,priority,last_checked,last_success,last_error,jobs_found,enabled FROM sources WHERE enabled=1 ORDER BY priority DESC,name").fetchall(); conn.close()
            payload = []
            for row in rows:
                item = dict(row)
                if item["last_error"] in (SOURCE_EMPTY_ERROR, "页面未解析到匹配岗位"):
                    item["state"] = "empty"
                elif item["last_error"]:
                    item["state"] = "error"
                elif item["last_success"]:
                    item["state"] = "ok"
                else:
                    item["state"] = "pending"
                payload.append(item)
            self.send_json(200, payload)
        elif path in ("/api/health", "/health"):
            conn = db_connect()
            last = conn.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
            extra_size = conn.execute("SELECT value FROM meta WHERE key='extra_pool_size'").fetchone()
            extra_cursor = conn.execute("SELECT value FROM meta WHERE key='extra_pool_cursor'").fetchone()
            jobpro_size = conn.execute("SELECT value FROM meta WHERE key='jobpro_size'").fetchone()
            jobpro_cursor = conn.execute("SELECT value FROM meta WHERE key='jobpro_cursor'").fetchone()
            conn.close()
            last_refresh = int(last[0]) if last else None
            extra_pool_size = int(extra_size[0]) if extra_size else len(extra_sources())
            extra_pool_cursor = int(extra_cursor[0]) if extra_cursor else 0
            jobpro_pool_size = int(jobpro_size[0]) if jobpro_size else len(jobpro_sources())
            jobpro_pool_cursor = int(jobpro_cursor[0]) if jobpro_cursor else 0
            ai_config = get_ai_config()
            deepseek_configured = bool(ai_config["api_key"])
            ccswitch_key = bool(get_key_from_ccswitch())
            codex_available = bool(codex_executable())
            key_configured = deepseek_configured or ccswitch_key
            ai_transport = "deepseek-openai" if deepseek_configured else "codex-cli / CCSwitch"
            self.send_json(200, {
                "status": "ok", "local": True, "jobs": len(jobs_json()),
                "ai_transport": ai_transport, "ai_key_configured": key_configured,
                "api_key_configured": key_configured, "codex_available": codex_available,
                "ai_ready": deepseek_configured or (key_configured and codex_available),
                "model": ai_config["model"],
                "last_refresh": last_refresh,
                "last_refresh_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_refresh)) if last_refresh else None,
                "refresh_interval_hours": REFRESH_SECONDS // 3600,
                "refresh_interval_minutes": REFRESH_SECONDS // 60,
                "refresh_interval_seconds": REFRESH_SECONDS,
                "refresh_in_progress": REFRESH_LOCK.locked(),
                "extra_pool_enabled": EXTRA_POOL_ENABLED,
                "extra_pool_size": extra_pool_size,
                "extra_pool_cursor": extra_pool_cursor,
                "extra_batch_size": EXTRA_BATCH_SIZE,
                "firecrawl_configured": bool(FIRECRAWL_API_KEY),
                "anysearch_enabled": ANYSEARCH_ENABLED,
                "jobpro_enabled": JOBPRO_ENABLED,
                "jobpro_size": jobpro_pool_size,
                "jobpro_cursor": jobpro_pool_cursor,
                "jobpro_batch_size": JOBPRO_BATCH_SIZE,
            })
        elif path in self.ALLOWED_STATIC:
            self.serve_static("/index.html" if path == "/" else path)
        else:
            self.send_json(404, {"error": "not found"})

    def serve_static(self, path: str) -> None:
        file_path = ROOT / path.lstrip("/")
        if not file_path.is_file() or file_path.parent != ROOT:
            self.send_json(404, {"error": "not found"}); return
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-cache"); self.end_headers(); self.wfile.write(content)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self.read_body()
            if path in ("/api/chat", "/chat"):
                answer = run_codex(body.get("messages", []), body.get("attachments", []))
                self.send_json(200, {"choices": [{"message": {"role": "assistant", "content": answer}}], "transport": "codex-cli"})
            elif path == "/api/profile/ai":
                self.send_json(200, parse_profile_with_ai(body.get("attachments")))
            elif path in ("/api/profile", "/api/profile/analyze"):
                payload = body.get("profile")
                if not isinstance(payload, dict):
                    raise ValueError("请提供结构化 profile；自动简历提取接口暂未启用")
                profile = normalize_profile(payload, current_profile())
                save_profile(profile)
                if path.endswith("/analyze"):
                    self.send_json(200, {"status": "accepted", "automatic_ai": False, "profile": profile})
                else:
                    self.send_json(200, profile)
            elif path == "/api/tracking":
                self.send_json(200, save_tracking(body))
            elif path == "/api/ai/config":
                self.send_json(200, save_ai_config(body))
            elif path == "/api/refresh":
                self.send_json(200, refresh_all())
            else:
                self.send_json(404, {"error": "not found"})
        except Exception as exc:
            if isinstance(exc, UpstreamUnavailable):
                status, kind = 503, "upstream_unavailable"
            elif isinstance(exc, (ValueError, json.JSONDecodeError)):
                status, kind = 400, "bad_request"
            else:
                status, kind = 502, "local_error"
            self.send_json(status, {"error": str(exc), "kind": kind})

    def log_message(self, *_args):
        return


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        # Dual-stack: accept both [::1] (localhost over IPv6) and IPv4 clients.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def get_key_from_ccswitch() -> str:
    """Read only the active provider's key for the fallback health indicator.

    Normal AI requests go through Codex CLI, which owns credential handling.
    """
    db_path = Path.home() / ".cc-switch" / "cc-switch.db"
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        row = conn.execute("SELECT settings_config FROM providers WHERE app_type='codex' AND is_current=1 ORDER BY rowid DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            return ""
        config = json.loads(row[0] or "{}")
        auth = config.get("auth") if isinstance(config, dict) else {}
        if not isinstance(auth, dict):
            return ""
        for name, value in auth.items():
            if "KEY" in str(name).upper() and isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    except Exception:
        return ""


def main() -> None:
    init_db()
    threading.Thread(target=refresh_loop, daemon=True, name="source-refresh").start()
    server = ReusableHTTPServer((HOST, PORT), RadarHandler)
    print(f"秋招雷达运行于 http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
