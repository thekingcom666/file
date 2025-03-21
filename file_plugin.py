# encoding:utf-8

"""File Plugin
A plugin for sending various types of files.
"""

# 标准库导入
import os
import json
import re
import glob
import shutil
import tempfile
import hashlib
import urllib.parse
from typing import Dict, List, Optional, Union, Tuple, Any
from datetime import datetime
import traceback
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socketserver
import random
import time
import base64
import socket

# 第三方库导入
import requests
from requests.exceptions import RequestException
from tqdm import tqdm

# 项目内部导入
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from common.log import logger
import plugins
from plugins import Plugin, Event, EventContext, EventAction

class FileServerHandler(SimpleHTTPRequestHandler):
    """文件服务处理器"""
    
    def __init__(self, *args, root_dir=None, **kwargs):
        self.root_dir = root_dir
        super().__init__(*args, **kwargs)
        
    def translate_path(self, path):
        """将URL路径转换为本地文件路径"""
        path = super().translate_path(path)
        if self.root_dir:
            # 将默认路径替换为指定的根目录
            rel_path = os.path.relpath(path, os.getcwd())
            return os.path.join(self.root_dir, rel_path)
        return path
        
    def log_message(self, format, *args):
        """重写日志方法，使用logger输出"""
        logger.debug(f"[FileServer] {format%args}")

@plugins.register(
    name="FilePlugin",
    desire_priority=100,
    desc="发送各种类型的文件",
    version="1.0",
    author="biubiu",
)
class FilePlugin(Plugin):
    """文件发送插件
    
    用于发送各种类型的文件，支持本地文件和网络文件。
    """

    def __init__(self):
        super().__init__()
        try:
            # 加载配置文件
            conf = super().load_config()
            if not conf:
                # 配置不存在则写入默认配置
                config_path = os.path.join(os.path.dirname(__file__), "config.json")
                if not os.path.exists(config_path):
                    # 默认配置
                    conf = {
                        "api": {
                            "token": "gewechat_token",
                            "base_url": "gewechat_base_url",
                            "app_id": "gewechat_app_id"
                        },
                        "settings": {
                            "default_dir": "./files",
                            "max_file_size": 100,
                            "allowed_extensions": [
                                "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                                "txt", "csv", "json", "xml", "html", "md",
                                "jpg", "jpeg", "png", "gif", "bmp", "svg",
                                "mp3", "wav", "ogg", "mp4", "avi", "mov",
                                "zip", "rar", "7z", "tar", "gz"
                            ],
                            "use_cache": True,
                            "cache_dir": "./cache",
                            "file_server": {
                                "host": "0.0.0.0",
                                "port": 0,  # 0表示使用随机端口
                                "public_host": "127.0.0.1"  # 外部访问的主机名
                            },
                            "file_retention": {
                                "default_days": 30,
                                "cache_expire_days": 3,
                                "clean_interval_days": 1
                            }
                        },
                        "shortcuts": {}
                    }
                    # 创建必要的目录
                    os.makedirs(conf["settings"]["default_dir"], exist_ok=True)
                    os.makedirs(conf["settings"]["cache_dir"], exist_ok=True)
                    
                    # 写入配置文件
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(conf, f, indent=4, ensure_ascii=False)

            # 保存配置
            self.api_config = conf.get("api", {})
            self.settings = conf.get("settings", {})
            self.shortcuts = conf.get("shortcuts", {})
            
            # 确保必要的目录存在
            os.makedirs(self.settings.get("default_dir", "./files"), exist_ok=True)
            os.makedirs(self.settings.get("cache_dir", "./cache"), exist_ok=True)
            
            # 启动文件服务器
            self._start_file_server()
            
            # 初始化文件保留计划
            self.retain_files = {}
            
            # 启动文件清理线程
            self._start_file_cleaner()
            
            # 注册事件处理函数
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            
            logger.info("[FilePlugin] 初始化成功")
        except Exception as e:
            logger.warn(f"[FilePlugin] 初始化失败: {e}")
            raise e

    def _start_file_server(self):
        """启动文件服务器"""
        try:
            server_config = self.settings.get("file_server", {})
            host = server_config.get("host", "0.0.0.0")
            port = server_config.get("port", 0)
            
            # 确保目录存在
            default_dir = os.path.abspath(self.settings.get("default_dir", "./files"))
            os.makedirs(default_dir, exist_ok=True)
            
            # 尝试允许自动选择端口如果指定端口被占用
            max_port_retry = 3
            port_retry_count = 0
            server_started = False
            
            while not server_started and port_retry_count < max_port_retry:
                try:
                    # 创建服务器
                    handler = lambda *args: FileServerHandler(*args, root_dir=default_dir)
                    self.httpd = socketserver.TCPServer((host, port), handler)
                    server_started = True
                except OSError as e:
                    # 如果端口被占用，尝试其他端口
                    if e.errno == 98 or "Address already in use" in str(e):  # 端口被占用
                        port_retry_count += 1
                        # 如果指定了端口，尝试使用随机端口
                        if port != 0:
                            logger.warning(f"[FilePlugin] 端口 {port} 已被占用，尝试随机端口")
                            port = 0
                        else:
                            # 如果已经是随机端口，等待一秒再试
                            logger.warning(f"[FilePlugin] 尝试绑定端口失败: {e}，等待后重试")
                            time.sleep(1)
                    else:
                        # 其他错误，抛出异常
                        raise
            
            if not server_started:
                raise Exception(f"无法启动文件服务器，已尝试 {max_port_retry} 次")
            
            # 获取实际使用的端口
            _, self.server_port = self.httpd.server_address
            logger.debug(f"[FilePlugin] 文件服务器绑定端口: {self.server_port}")
            
            # 获取配置和环境变量中的外部访问地址
            public_host = server_config.get("public_host", None)
            env_public_host = os.environ.get("PUBLIC_HOST", None)
            
            # 构建可能的主机地址列表
            possible_hosts = []
            
            # 1. 首先检查环境变量中是否指定了公共主机名
            if env_public_host and env_public_host != "127.0.0.1" and env_public_host != "localhost":
                possible_hosts.append(env_public_host)
                logger.debug(f"[FilePlugin] 从环境变量获取到公共主机: {env_public_host}")
                
            # 2. 然后检查API地址的主机部分（微信API服务器地址，通常是可以从外部访问的）
            base_url = self.api_config.get("base_url", "")
            if base_url:
                try:
                    from urllib.parse import urlparse
                    parsed_url = urlparse(base_url)
                    api_host = parsed_url.netloc.split(':')[0]
                    if api_host and api_host != "127.0.0.1" and api_host != "localhost":
                        possible_hosts.append(api_host)
                        logger.debug(f"[FilePlugin] 从API地址获取到主机: {api_host}")
                except Exception as e:
                    logger.warning(f"[FilePlugin] 解析API地址失败: {e}")
                    
            # 3. 检查配置中的公共主机名
            if public_host and public_host != "127.0.0.1" and public_host != "localhost":
                if public_host not in possible_hosts:
                    possible_hosts.append(public_host)
                logger.debug(f"[FilePlugin] 从配置获取到公共主机: {public_host}")
                
            # 4. 尝试获取系统环境变量中的主机名
            try:
                hostname = os.environ.get("HOSTNAME", os.environ.get("COMPUTERNAME", socket.gethostname()))
                if hostname and hostname != "localhost":
                    possible_hosts.append(hostname)
                    logger.debug(f"[FilePlugin] 从系统获取到主机名: {hostname}")
            except Exception as e:
                logger.warning(f"[FilePlugin] 获取系统主机名失败: {e}")
                
            # 5. 尝试通过DNS查询获取主机的公共IP
            try:
                import socket
                hostname = socket.gethostname()
                # 尝试获取公共IP
                host_ip = socket.gethostbyname(hostname)
                if host_ip and host_ip != "127.0.0.1" and host_ip not in possible_hosts:
                    possible_hosts.append(host_ip)
                    logger.debug(f"[FilePlugin] 通过DNS获取到主机IP: {host_ip}")
            except Exception as e:
                logger.warning(f"[FilePlugin] 通过DNS获取主机IP失败: {e}")
                
            # 6. 尝试通过UDP获取本机IP
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                udp_ip = s.getsockname()[0]
                s.close()
                if udp_ip not in possible_hosts:
                    possible_hosts.append(udp_ip)
                logger.debug(f"[FilePlugin] UDP连接获取到IP: {udp_ip}")
            except Exception as e:
                logger.warning(f"[FilePlugin] UDP获取IP失败: {e}")
                
            # 7. 尝试获取网卡地址
            try:
                import netifaces
                for interface in netifaces.interfaces():
                    addrs = netifaces.ifaddresses(interface)
                    if netifaces.AF_INET in addrs:
                        for addr in addrs[netifaces.AF_INET]:
                            ip = addr['addr']
                            if ip != '127.0.0.1' and not ip.startswith('169.254') and ip not in possible_hosts:
                                possible_hosts.append(ip)
                                logger.debug(f"[FilePlugin] 从网卡 {interface} 获取到IP: {ip}")
            except ImportError:
                logger.debug("[FilePlugin] netifaces库未安装，跳过网卡地址获取")
            except Exception as e:
                logger.warning(f"[FilePlugin] 获取网卡地址失败: {e}")
                
            # 添加Docker主机地址作为备选
            try:
                # 尝试读取/etc/hosts文件查找docker host
                with open('/etc/hosts', 'r') as hosts_file:
                    for line in hosts_file:
                        if 'host.docker.internal' in line:
                            parts = line.strip().split()
                            if parts and parts[0] != '127.0.0.1' and parts[0] not in possible_hosts:
                                possible_hosts.append(parts[0])
                                logger.debug(f"[FilePlugin] 从hosts文件获取到Docker主机IP: {parts[0]}")
                                break
            except Exception as e:
                logger.debug(f"[FilePlugin] 读取hosts文件失败: {e}")
                
            # 如果配置文件中指定了外部可访问的主机名或IP，优先使用
            external_host = server_config.get("external_host", None)
            if external_host and external_host not in possible_hosts:
                possible_hosts.insert(0, external_host)  # 插入到列表最前面
                logger.debug(f"[FilePlugin] 从配置获取到外部主机: {external_host}")
                
            # 添加本地回环地址作为最后备选
            if "127.0.0.1" not in possible_hosts:
                possible_hosts.append("127.0.0.1")
                
            # 如果没有找到可能的主机，使用默认值
            if not possible_hosts:
                possible_hosts.append("localhost")
                
            # 在新线程中启动服务器
            self.server_thread = threading.Thread(target=self.httpd.serve_forever)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            # 等待服务器启动
            time.sleep(0.5)
            
            # 测试候选主机的可访问性，选择第一个可用的
            found_accessible_host = False
            selected_host = None
            
            for host in possible_hosts:
                try:
                    test_url = f"http://{host}:{self.server_port}/"
                    logger.debug(f"[FilePlugin] 测试主机可访问性: {test_url}")
                    response = requests.head(test_url, timeout=2)
                    if response.status_code == 200:
                        logger.info(f"[FilePlugin] 找到可访问的主机: {host}")
                        selected_host = host
                        found_accessible_host = True
                        break
                except Exception as e:
                    logger.debug(f"[FilePlugin] 主机 {host} 测试失败: {e}")
                    
            # 如果没有找到可访问的主机，使用第一个非本地的IP，或默认使用127.0.0.1
            if not found_accessible_host:
                for host in possible_hosts:
                    if host != "127.0.0.1" and host != "localhost":
                        selected_host = host
                        logger.warning(f"[FilePlugin] 没有找到可访问的主机，使用: {host}")
                        break
                    
                if not selected_host:
                    selected_host = "127.0.0.1"
                    logger.warning("[FilePlugin] 所有主机测试失败，使用本地回环地址")
                    
            # 更新配置中的public_host
            public_host = selected_host
            self.settings["file_server"]["public_host"] = public_host
            
            # 保存配置
            try:
                config_path = os.path.join(os.path.dirname(__file__), "config.json")
                with open(config_path, "r+", encoding="utf-8") as f:
                    config = json.load(f)
                    config["settings"]["file_server"]["public_host"] = public_host
                    config["settings"]["file_server"]["port"] = self.server_port
                    f.seek(0)
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    f.truncate()
                logger.debug(f"[FilePlugin] 更新配置文件成功: {config_path}")
            except Exception as e:
                logger.error(f"[FilePlugin] 保存配置失败: {e}")
                
            logger.info(f"[FilePlugin] 文件服务器启动成功，监听地址: {public_host}:{self.server_port}")
            
            # 再次测试服务器可访问性，确保配置有效
            try:
                test_url = f"http://{public_host}:{self.server_port}/"
                response = requests.head(test_url, timeout=3)
                if response.status_code == 200:
                    logger.info(f"[FilePlugin] 文件服务器可正常访问: {test_url}")
                    
                    # 检查默认目录下的文件是否可访问
                    test_files = os.listdir(default_dir)
                    if test_files:
                        test_file = test_files[0]
                        test_file_url = f"http://{public_host}:{self.server_port}/{urllib.parse.quote(test_file)}"
                        try:
                            file_response = requests.head(test_file_url, timeout=2)
                            if file_response.status_code == 200:
                                logger.info(f"[FilePlugin] 文件可正常访问: {test_file_url}")
                            else:
                                logger.warning(f"[FilePlugin] 文件访问测试失败: {test_file_url}, 状态码: {file_response.status_code}")
                        except Exception as e:
                            logger.warning(f"[FilePlugin] 文件访问测试异常: {e}")
                else:
                    logger.warning(f"[FilePlugin] 文件服务器访问测试失败: {test_url}, 状态码: {response.status_code}")
            except Exception as e:
                logger.warning(f"[FilePlugin] 测试文件服务器访问失败: {e}")
                
                # 尝试使用本地地址测试，确认服务器是否实际运行
                try:
                    local_url = f"http://127.0.0.1:{self.server_port}/"
                    local_response = requests.head(local_url, timeout=1)
                    if local_response.status_code == 200:
                        logger.info(f"[FilePlugin] 文件服务器本地可访问: {local_url}")
                        logger.warning("[FilePlugin] 可能是网络隔离或防火墙问题导致外部无法访问")
                    else:
                        logger.error(f"[FilePlugin] 文件服务器本地测试也失败: {local_url}, 状态码: {local_response.status_code}")
                except Exception as local_e:
                    logger.error(f"[FilePlugin] 本地测试异常，服务器可能未正常运行: {local_e}")
                    
                    # 检查端口是否被占用
                    try:
                        import socket
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        result = sock.connect_ex(('127.0.0.1', self.server_port))
                        if result == 0:
                            logger.info(f"[FilePlugin] 端口 {self.server_port} 已被占用，可能是其他程序")
                        else:
                            logger.error(f"[FilePlugin] 端口 {self.server_port} 未被占用，服务器可能未启动")
                        sock.close()
                    except Exception as port_e:
                        logger.error(f"[FilePlugin] 端口检查失败: {port_e}")
            
        except Exception as e:
            logger.error(f"[FilePlugin] 启动文件服务器失败: {e}")
            logger.error(traceback.format_exc())
            # 不抛出异常，使插件可以继续工作，但文件服务器功能不可用
            self.httpd = None
            self.server_port = None

    def _get_file_url(self, file_path: str) -> str:
        """获取文件的HTTP URL
        
        Args:
            file_path: 本地文件路径
            
        Returns:
            文件的HTTP URL
        """
        try:
            # 确保文件服务器已启动
            if not hasattr(self, 'server_port') or self.server_port is None:
                logger.error("[FilePlugin] 文件服务器未启动，无法获取URL")
                return None
                
            # 获取相对于文件目录的路径
            root_dir = os.path.abspath(self.settings.get("default_dir"))
            rel_path = os.path.basename(file_path)  # 默认只使用文件名
            
            # 如果文件在根目录内，获取相对路径
            if os.path.abspath(file_path).startswith(root_dir):
                rel_path = os.path.relpath(file_path, root_dir)
            
            # 获取服务器配置
            server_config = self.settings.get("file_server", {})
            
            # 优先使用external_host作为外部访问地址
            host_to_use = server_config.get("external_host")
            
            # 如果external_host未配置，尝试使用public_host
            if not host_to_use:
                host_to_use = server_config.get("public_host", "127.0.0.1")
                
            # 如果主机是本地地址，尝试获取可能的外部IP
            if host_to_use == "127.0.0.1" or host_to_use == "localhost":
                # 尝试从API地址获取主机部分
                base_url = self.api_config.get("base_url", "")
                if base_url:
                    try:
                        from urllib.parse import urlparse
                        parsed_url = urlparse(base_url)
                        api_host = parsed_url.netloc.split(':')[0]
                        if api_host and api_host != "127.0.0.1" and api_host != "localhost":
                            host_to_use = api_host
                            logger.debug(f"[FilePlugin] 使用API主机作为URL地址: {host_to_use}")
                    except Exception as e:
                        logger.warning(f"[FilePlugin] 解析API地址失败: {e}")
            
            # 构建URL
            url = f"http://{host_to_use}:{self.server_port}/{urllib.parse.quote(rel_path)}"
            
            # 记录日志
            logger.debug(f"[FilePlugin] 生成文件URL: {url}")
            
            return url
                
        except Exception as e:
            logger.error(f"[FilePlugin] 生成文件URL失败: {e}")
            logger.error(traceback.format_exc())
            return None

    def _get_file_info(self, file_path: str) -> Dict[str, Any]:
        """获取文件信息
        
        Args:
            file_path: 文件路径
            
        Returns:
            包含文件信息的字典
        """
        try:
            # 标准化路径
            file_path = os.path.normpath(file_path)
            logger.debug(f"[FilePlugin] 获取文件信息: {file_path}")
            
            # 如果是相对路径，则相对于默认目录
            if not os.path.isabs(file_path):
                default_dir = self.settings.get("default_dir", "./files")
                default_path = os.path.join(default_dir, file_path)
                
                # 如果默认目录中不存在该文件，检查缓存目录
                if not os.path.exists(default_path) or not os.path.isfile(default_path):
                    cache_dir = self.settings.get("cache_dir", "./cache")
                    cache_path = os.path.join(cache_dir, os.path.basename(file_path))
                    
                    # 如果缓存目录中存在该文件，使用缓存路径
                    if os.path.exists(cache_path) and os.path.isfile(cache_path):
                        logger.debug(f"[FilePlugin] 在缓存目录中找到文件: {cache_path}")
                        file_path = cache_path
                    else:
                        file_path = default_path
                else:
                    file_path = default_path
            
            # 检查文件是否存在
            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                abs_path = os.path.abspath(file_path)
                logger.error(f"[FilePlugin] 文件不存在: {abs_path}")
                
                # 列出当前目录和缓存目录的文件，用于调试
                try:
                    default_dir = os.path.abspath(self.settings.get("default_dir", "./files"))
                    cache_dir = os.path.abspath(self.settings.get("cache_dir", "./cache"))
                    logger.debug(f"[FilePlugin] 默认目录: {default_dir}")
                    logger.debug(f"[FilePlugin] 缓存目录: {cache_dir}")
                    
                    if os.path.exists(default_dir):
                        files = os.listdir(default_dir)
                        logger.debug(f"[FilePlugin] 默认目录文件: {files}")
                        
                    if os.path.exists(cache_dir):
                        files = os.listdir(cache_dir)
                        logger.debug(f"[FilePlugin] 缓存目录文件: {files}")
                except Exception as e:
                    logger.error(f"[FilePlugin] 列出目录文件失败: {e}")
                
                return {"exists": False, "error": "文件不存在"}
            
            # 获取文件基本信息
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            file_ext = os.path.splitext(file_name)[1].lower().replace(".", "")
            
            # 检查文件大小
            max_size = self.settings.get("max_file_size", 100) * 1024 * 1024  # 转换为字节
            if file_size > max_size:
                return {
                    "exists": True,
                    "valid": False,
                    "error": f"文件大小({self._format_size(file_size)})超过限制({self._format_size(max_size)})"
                }
            
            # 检查文件类型
            allowed_exts = self.settings.get("allowed_extensions", [])
            if allowed_exts and file_ext not in allowed_exts:
                return {
                    "exists": True,
                    "valid": False,
                    "error": f"不支持的文件类型: {file_ext}"
                }
            
            # 获取文件修改时间
            mod_time = os.path.getmtime(file_path)
            mod_time_str = datetime.fromtimestamp(mod_time).strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取文件MIME类型
            mime_type = self._get_mime_type(file_ext)
            
            return {
                "exists": True,
                "valid": True,
                "path": file_path,
                "name": file_name,
                "size": file_size,
                "size_str": self._format_size(file_size),
                "extension": file_ext,
                "mime_type": mime_type,
                "modified": mod_time_str
            }
        except Exception as e:
            logger.error(f"[FilePlugin] 获取文件信息失败: {e}")
            return {"exists": False, "error": f"获取文件信息失败: {str(e)}"}

    def _get_mime_type(self, extension: str) -> str:
        """根据文件扩展名获取MIME类型
        
        Args:
            extension: 文件扩展名（不含点）
            
        Returns:
            MIME类型字符串
        """
        # 常见MIME类型映射
        mime_types = {
            # 文档
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "txt": "text/plain",
            "csv": "text/csv",
            "json": "application/json",
            "xml": "application/xml",
            "html": "text/html",
            "md": "text/markdown",
            
            # 图片
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "svg": "image/svg+xml",
            
            # 音频
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            
            # 视频
            "mp4": "video/mp4",
            "avi": "video/x-msvideo",
            "mov": "video/quicktime",
            
            # 压缩文件
            "zip": "application/zip",
            "rar": "application/x-rar-compressed",
            "7z": "application/x-7z-compressed",
            "tar": "application/x-tar",
            "gz": "application/gzip"
        }
        
        return mime_types.get(extension.lower(), "application/octet-stream")

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小
        
        Args:
            size_bytes: 文件大小（字节）
            
        Returns:
            格式化后的大小字符串
        """
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f"{size_bytes:.2f}{unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f}PB"

    def _download_file(self, url: str, save_path: str = None) -> Optional[str]:
        """下载网络文件
        
        Args:
            url: 文件URL
            save_path: 保存路径，如果不指定则保存到临时目录
            
        Returns:
            文件保存路径，下载失败则返回None
        """
        try:
            logger.debug(f"[FilePlugin] 下载文件: {url}")
            # 如果未指定保存路径，则保存到临时目录
            if not save_path:
                # 从URL中提取文件名
                file_name = os.path.basename(urllib.parse.urlparse(url).path)
                # 如果URL中没有有效的文件名，尝试从查询参数获取
                if not file_name:
                    query = urllib.parse.urlparse(url).query
                    params = urllib.parse.parse_qs(query)
                    for param in ['filename', 'name', 'file', 'download']:
                        if param in params and params[param]:
                            file_name = params[param][0]
                            logger.debug(f"[FilePlugin] 从查询参数获取文件名: {file_name}")
                            break
                
                # 如果仍未获取到文件名，使用MD5作为文件名
                if not file_name:
                    file_name = hashlib.md5(url.encode()).hexdigest()[:8]
                    
                    # 尝试从Content-Type或Content-Disposition推断扩展名
                    try:
                        head_response = requests.head(url, timeout=5)
                        
                        # 从Content-Disposition获取文件名
                        cd = head_response.headers.get('content-disposition')
                        if cd:
                            import re
                            fname = re.findall('filename=(.+)', cd)
                            if fname:
                                file_name = fname[0].strip('"\'')
                                logger.debug(f"[FilePlugin] 从Content-Disposition获取文件名: {file_name}")
                        
                        # 从Content-Type获取扩展名
                        content_type = head_response.headers.get('content-type', '')
                        if not file_name or '.' not in file_name:
                            if 'pdf' in content_type:
                                file_name += '.pdf'
                            elif 'image/jpeg' in content_type:
                                file_name += '.jpg'
                            elif 'image/png' in content_type:
                                file_name += '.png'
                            elif 'image/gif' in content_type:
                                file_name += '.gif'
                            elif 'application/msword' in content_type:
                                file_name += '.doc'
                            elif 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' in content_type:
                                file_name += '.docx'
                            elif 'application/vnd.ms-excel' in content_type:
                                file_name += '.xls'
                            elif 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in content_type:
                                file_name += '.xlsx'
                            elif 'text/plain' in content_type:
                                file_name += '.txt'
                            elif 'text/html' in content_type:
                                file_name += '.html'
                            elif 'application/zip' in content_type:
                                file_name += '.zip'
                            # 确保有扩展名
                            elif '.' not in file_name:
                                file_name += '.dat'
                    except Exception as e:
                        logger.warning(f"[FilePlugin] 获取文件头信息失败: {e}")
                        # 默认为dat扩展名
                        file_name += '.dat'
                        
                save_path = os.path.join(self.settings.get("cache_dir", "./cache"), file_name)
            
            # 创建保存目录
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # 下载文件
            logger.debug(f"[FilePlugin] 开始下载: {url} -> {save_path}")
            
            # 设置重试机制
            session = requests.Session()
            retries = 3
            backoff_factor = 0.5
            retry = requests.packages.urllib3.util.retry.Retry(
                total=retries,
                backoff_factor=backoff_factor,
                status_forcelist=[500, 502, 503, 504]
            )
            session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retry))
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retry))
            
            response = session.get(url, stream=True, timeout=30)
            response.raise_for_status()
            
            # 获取文件大小
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            
            # 获取文件大小
            total_size = int(response.headers.get('content-length', 0))
            logger.debug(f"[FilePlugin] 文件大小: {self._format_size(total_size)}")
            
            # 检查文件大小是否超过限制
            max_size = self.settings.get("max_file_size", 100) * 1024 * 1024
            if total_size > max_size:
                logger.error(f"[FilePlugin] 文件大小({self._format_size(total_size)})超过限制({self._format_size(max_size)})")
                return None
            
            # 写入文件
            with open(save_path, 'wb') as f:
                downloaded_size = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                logger.debug(f"[FilePlugin] 下载完成: {self._format_size(downloaded_size)}")
            
            # 将文件标记为长期保留
            self._mark_file_retained(save_path)
            
            return save_path
            
        except Exception as e:
            logger.error(f"[FilePlugin] 下载文件失败: {e}")
            logger.error(traceback.format_exc())
            return None

    def _mark_file_retained(self, file_path: str, days: int = None) -> None:
        """标记文件为长期保留
        
        Args:
            file_path: 文件路径
            days: 保留天数，如果不指定则使用配置中的默认值
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                logger.error(f"[FilePlugin] 无法标记不存在的文件为长期保留: {file_path}")
                return
            
            # 获取默认保留天数
            if days is None:
                retention_config = self.settings.get("file_retention", {})
                days = retention_config.get("default_days", 30)
                
            # 复制文件到default_dir，确保长期可访问
            default_dir = self.settings.get("default_dir", "./files")
            file_name = os.path.basename(file_path)
            target_path = os.path.join(default_dir, file_name)
            
            # 如果文件已经在default_dir，则不需要复制
            if os.path.abspath(file_path) != os.path.abspath(target_path):
                shutil.copy2(file_path, target_path)
                logger.debug(f"[FilePlugin] 复制文件到长期存储区: {target_path}")
                
            # 记录保留信息
            self.retain_files[target_path] = {
                "original_path": file_path,
                "expire_time": datetime.now().timestamp() + days * 86400  # days转换为秒
            }
            
            logger.debug(f"[FilePlugin] 标记文件为长期保留({days}天): {target_path}")
            
        except Exception as e:
            logger.error(f"[FilePlugin] 标记文件为长期保留失败: {e}")
            logger.error(traceback.format_exc())

    def _search_files(self, keyword: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """搜索文件
        
        Args:
            keyword: 搜索关键词
            max_results: 最大结果数
            
        Returns:
            文件信息列表
        """
        results = []
        try:
            # 在默认目录中搜索文件
            search_dir = self.settings.get("default_dir", "./files")
            pattern = f"*{keyword}*"
            
            # 使用glob进行搜索
            matched_files = []
            for ext in self.settings.get("allowed_extensions", []):
                matched_files.extend(glob.glob(os.path.join(search_dir, f"*{keyword}*.{ext}")))
                matched_files.extend(glob.glob(os.path.join(search_dir, "**", f"*{keyword}*.{ext}"), recursive=True))
            
            # 去重
            matched_files = list(set(matched_files))
            
            # 仅保留文件（不包括目录）
            matched_files = [f for f in matched_files if os.path.isfile(f)]
            
            # 获取详细信息
            for file_path in matched_files[:max_results]:
                file_info = self._get_file_info(file_path)
                if file_info.get("exists", False) and file_info.get("valid", False):
                    results.append(file_info)
                
        except Exception as e:
            logger.error(f"[FilePlugin] 搜索文件失败: {e}")
            
        return results

    def _send_file(self, to_wxid: str, file_info: dict, is_network_file: bool = False) -> bool:
        """发送文件
        
        Args:
            to_wxid: 目标微信ID
            file_info: 文件信息字典
            is_network_file: 是否为网络文件
            
        Returns:
            发送是否成功
        """
        try:
            if not file_info.get("exists", True):
                error_msg = file_info.get("error", "文件不存在")
                logger.error(f"[FilePlugin] {error_msg}")
                return False
            
            # 检查文件是否存在
            if not os.path.exists(file_info.get("path", "")):
                logger.error(f"[FilePlugin] 文件不存在: {file_info.get('path')}")
                return False
                
            # 检查文件大小
            max_size_mb = self.settings.get("max_file_size", 100)
            max_size = max_size_mb * 1024 * 1024
            
            if file_info.get("size", 0) > max_size:
                size_str = self._format_size(file_info["size"])
                max_size_str = self._format_size(max_size)
                logger.error(f"[FilePlugin] 文件大小超过限制: {size_str}/{max_size_str}")
                return False
                
            # 确保文件在默认目录中，便于访问
            if not is_network_file:
                default_dir = os.path.abspath(self.settings.get("default_dir", "./files"))
                if not file_info["path"].startswith(default_dir):
                    try:
                        # 复制文件到默认目录
                        os.makedirs(default_dir, exist_ok=True)
                        new_path = os.path.join(default_dir, os.path.basename(file_info["path"]))
                        
                        if new_path != file_info["path"]:  # 避免相同路径复制
                            shutil.copy2(file_info["path"], new_path)
                            logger.debug(f"[FilePlugin] 复制文件到默认目录: {new_path}")
                            file_info["path"] = new_path
                        else:
                            logger.debug(f"[FilePlugin] 文件已在默认目录中: {new_path}")
                    except Exception as e:
                        logger.error(f"[FilePlugin] 复制文件失败: {e}")
                            
            logger.info(f"[FilePlugin] 正在发送文件: {file_info['name']} ({file_info['size_str']})")
            
            # 标记文件为长期保留
            self._mark_file_retained(file_info['path'])
            
            # 读取文件的二进制内容，用于后续发送
            try:
                with open(file_info['path'], 'rb') as f:
                    file_content = f.read()
                    file_content_md5 = hashlib.md5(file_content).hexdigest()
                    logger.debug(f"[FilePlugin] 文件内容MD5: {file_content_md5}, 大小: {len(file_content)}字节")
                    
                    # 检查文件内容是否为空
                    if len(file_content) == 0:
                        logger.error(f"[FilePlugin] 文件内容为空: {file_info['path']}")
                        return False
                        
                    # 检查内容大小与文件大小是否一致
                    if len(file_content) != file_info['size']:
                        logger.warning(f"[FilePlugin] 读取的内容大小 ({len(file_content)}) 与文件大小 ({file_info['size']}) 不一致")
            except Exception as e:
                logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                return False
            
            # 确保文件信息中包含文件内容
            file_info['content'] = file_content
            
            # 构建一个发送方法列表，从最可能成功的开始尝试
            # 优先使用不依赖URL的方法，因为文件服务器可能不可用
            server_likely_unavailable = self.httpd is None or self.server_port is None
            
            send_methods = []
            
            # 1. 直接二进制上传 - 最可靠，不依赖文件服务器
            send_methods.append({
                "name": "direct_upload",
                "function": self._try_direct_upload,
                "desc": "二进制直传"
            })
            
            # 2. 如果文件较小，尝试内联发送
            if file_info['size'] < 5 * 1024 * 1024:  # 5MB以下
                send_methods.append({
                    "name": "inline",
                    "function": self._try_inline_file,
                    "desc": "内联发送"
                })
            
            # 3. 尝试附件方式发送
            send_methods.append({
                "name": "attachment",
                "function": self._try_send_as_attachment,
                "desc": "附件方式发送"
            })
            
            # 4. 尝试转发文件
            send_methods.append({
                "name": "forward",
                "function": self._try_forward_file,
                "desc": "转发接口发送"
            })
            
            # 5. 仅当文件服务器可用时，才使用这些依赖URL的方法
            if not server_likely_unavailable:
                send_methods.append({
                    "name": "post",
                    "function": self._try_post_file,
                    "desc": "POST接口发送"
                })
                
                send_methods.append({
                    "name": "upload",
                    "function": self._try_upload_file,
                    "desc": "上传文件发送"
                })
            
            # 6. 组合方法：结合二进制和URL的优势
            send_methods.append({
                "name": "combined",
                "function": self._try_combined_method,
                "desc": "组合方法发送"
            })
                
            # 尝试所有可能的发送方法
            for method in send_methods:
                try:
                    logger.debug(f"[FilePlugin] 尝试使用 {method['desc']} 发送文件")
                    if method.get("condition", True):  # 检查条件是否满足
                        if method["function"](to_wxid, file_info):
                            logger.info(f"[FilePlugin] 使用 {method['desc']} 发送文件成功")
                            return True
                    else:
                        logger.debug(f"[FilePlugin] 跳过 {method['desc']}，条件不满足")
                except Exception as e:
                    logger.error(f"[FilePlugin] {method['desc']} 发送失败: {e}")
                    logger.error(traceback.format_exc())
            
            logger.error(f"[FilePlugin] 所有发送方法都失败，无法发送文件: {file_info['name']}")
            return False
        except Exception as e:
            logger.error(f"[FilePlugin] 发送文件过程中出错: {e}")
            logger.error(traceback.format_exc())
            return False

    def _try_combined_method(self, to_wxid: str, file_info: dict) -> bool:
        """组合方法发送文件，结合二进制内容和URL

        Args:
            to_wxid: 目标微信ID
            file_info: 文件信息

        Returns:
            发送是否成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试组合方法发送文件: {file_info['name']}")
            
            # 获取文件内容，如果没有预先读取，则在这里读取
            file_content = file_info.get('content')
            if file_content is None:
                try:
                    with open(file_info['path'], 'rb') as f:
                        file_content = f.read()
                        file_info['content'] = file_content
                except Exception as e:
                    logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                    return False
            
            # 使用api_config获取base_url
            base_url = self.api_config.get('base_url')
            if not base_url:
                logger.error("[FilePlugin] 未配置base_url，无法进行组合方法发送")
                return False
            
            # 构建请求数据
            file_name = file_info['name']
            mime_type = file_info['mime_type']
            
            # 尝试通过上传API发送文件内容
            try:
                upload_url = f"{base_url}/message/uploadFile"
                logger.debug(f"[FilePlugin] 尝试上传文件到: {upload_url}")
                
                # 准备表单数据
                files = {
                    'file': (file_name, file_content, mime_type)
                }
                
                # 添加必要的元数据
                data = {
                    'app_id': self.api_config.get('app_id'),
                    'target_id': to_wxid,
                    'file_name': file_name,
                    'file_type': mime_type,
                    'file_size': len(file_content)
                }
                
                headers = {
                    'Token': self.api_config.get('token')
                }
                
                # 设置较长的超时时间
                response = requests.post(upload_url, headers=headers, data=data, files=files, timeout=60)
                if response.status_code == 200:
                    result = response.json()
                    if result.get('code') == 200:
                        logger.info(f"[FilePlugin] 文件上传成功: {result.get('data')}")
                        return True
                    else:
                        error = result.get('message', '未知错误')
                        logger.error(f"[FilePlugin] 文件上传失败: {error}")
                else:
                    logger.error(f"[FilePlugin] 文件上传请求失败，状态码: {response.status_code}")
            except Exception as e:
                logger.error(f"[FilePlugin] 上传文件异常: {e}")
                
            # 如果上传失败，尝试使用sendUrl方法
            try:
                # 获取文件URL
                file_url = None
                if self.httpd and self.server_port:
                    file_url = self._get_file_url(file_info['path'])
                
                # 如果没有文件服务器或无法获取URL，尝试先上传到临时服务然后获取URL
                if not file_url:
                    try:
                        temp_upload_url = f"{base_url}/file/upload"
                        temp_files = {
                            'file': (file_name, file_content, mime_type)
                        }
                        temp_response = requests.post(temp_upload_url, files=temp_files, timeout=60)
                        
                        if temp_response.status_code == 200:
                            temp_result = temp_response.json()
                            if temp_result.get('code') == 200:
                                file_url = temp_result.get('data', {}).get('url')
                                logger.debug(f"[FilePlugin] 临时上传获取URL成功: {file_url}")
                            else:
                                logger.error(f"[FilePlugin] 临时上传请求失败: {temp_response.status_code}")
                        else:
                            logger.error(f"[FilePlugin] 临时上传请求失败: {temp_response.status_code}")
                    except Exception as e:
                        logger.error(f"[FilePlugin] 临时上传异常: {e}")
                
                # 如果获取到URL，尝试发送URL
                if file_url:
                    send_url = f"{base_url}/message/sendUrl"
                    
                    payload = {
                        'app_id': self.api_config.get('app_id'),
                        'target_id': to_wxid,
                        'url': file_url,
                        'title': file_name,
                        'describe': f"文件大小: {file_info['size_str']}, 类型: {mime_type}"
                    }
                    
                    headers = {
                        'Token': self.api_config.get('token'),
                        'Content-Type': 'application/json'
                    }
                    
                    url_response = requests.post(send_url, headers=headers, json=payload, timeout=10)
                    if url_response.status_code == 200:
                        url_result = url_response.json()
                        if url_result.get('code') == 200:
                            logger.info(f"[FilePlugin] 通过URL发送文件成功: {file_url}")
                            return True
                        else:
                            error = url_result.get('message', '未知错误')
                            logger.error(f"[FilePlugin] 通过URL发送文件失败: {error}")
                    else:
                        logger.error(f"[FilePlugin] URL发送请求失败，状态码: {url_response.status_code}")
            except Exception as e:
                logger.error(f"[FilePlugin] 通过URL发送异常: {e}")
                
            # 所有方法都失败
            return False
        except Exception as e:
            logger.error(f"[FilePlugin] 组合方法发送失败: {e}")
            logger.error(traceback.format_exc())
            return False

    def _try_send_as_attachment(self, to_wxid: str, file_info: dict) -> bool:
        """尝试使用消息附件方式发送文件
        
        这种方式更接近原生的微信发送文件方式，可能更可靠
        
        Args:
            to_wxid: 接收者ID
            file_info: 文件信息
            
        Returns:
            是否发送成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试使用消息附件方式发送文件: {file_info['name']}")
            
            # 确保有有效的文件路径
            if not os.path.exists(file_info['path']):
                logger.error(f"[FilePlugin] 文件不存在: {file_info['path']}")
                return False
            
            # 构建发送附件消息的数据
            data = {
                "appId": self.api_config.get("app_id"),
                "toWxid": to_wxid,
                "path": file_info['path'],
                "fileName": file_info['name']
            }
            
            # 发送请求
            response = requests.post(
                f"{self.api_config.get('base_url')}/message/sendFileMsg",
                json=data,
                headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                timeout=30
            )
            
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result.get("ret") == 200:
                    logger.info(f"[FilePlugin] 附件方式发送成功: {file_info['name']}")
                    return True
                else:
                    logger.error(f"[FilePlugin] 附件方式发送失败: {result.get('msg')}")
            else:
                logger.error(f"[FilePlugin] 附件方式API请求失败: {response.status_code}")
                try:
                    logger.error(f"[FilePlugin] 响应内容: {response.text}")
                except:
                    logger.error("[FilePlugin] 无法读取响应内容")
            
            # 尝试备用API
            data = {
                "appId": self.api_config.get("app_id"),
                "toWxid": to_wxid,
                "filePath": file_info['path']
            }
            
            response = requests.post(
                f"{self.api_config.get('base_url')}/message/sendFile",
                json=data,
                headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("ret") == 200:
                    logger.info(f"[FilePlugin] 备用API发送成功: {file_info['name']}")
                    return True
                else:
                    logger.error(f"[FilePlugin] 备用API发送失败: {result.get('msg')}")
            else:
                logger.error(f"[FilePlugin] 备用API请求失败: {response.status_code}")
            
        except Exception as e:
            logger.error(f"[FilePlugin] 附件方式发送失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            
        return False

    def _try_direct_upload(self, to_wxid: str, file_info: dict) -> bool:
        """尝试使用二进制直传接口发送文件
        
        直接上传文件二进制内容，不依赖文件服务器URL
        
        Args:
            to_wxid: 目标微信ID
            file_info: 文件信息
            
        Returns:
            发送是否成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试二进制直传: {file_info['name']}")
            
            # 获取文件内容
            file_content = file_info.get('content')
            if file_content is None:
                # 如果之前没有读取过文件内容，现在读取
                try:
                    with open(file_info['path'], 'rb') as f:
                        file_content = f.read()
                        if not file_content:
                            logger.error(f"[FilePlugin] 文件内容为空: {file_info['path']}")
                            return False
                        file_info['content'] = file_content
                        logger.debug(f"[FilePlugin] 读取文件内容成功，大小: {len(file_content)}字节")
                except Exception as e:
                    logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                    return False
            
            # 计算MD5值，用于日志和调试
            file_md5 = hashlib.md5(file_content).hexdigest()
            logger.debug(f"[FilePlugin] 文件内容MD5: {file_md5}, 大小: {len(file_content)}字节")
            
            # 使用api_config获取base_url
            base_url = self.api_config.get('base_url')
            if not base_url:
                logger.error("[FilePlugin] 未配置base_url，无法进行二进制直传")
                return False
            
            # 尝试不同的API端点
            upload_endpoints = [
                {
                    "url": f"{base_url}/message/uploadFile",
                    "desc": "标准上传接口",
                    "headers": {
                        "Token": self.api_config.get("token")
                    },
                    "file_param": "file",
                    "data": {
                        "app_id": self.api_config.get("app_id"),
                        "target_id": to_wxid,
                        "file_name": file_info['name'],
                        "file_type": file_info['mime_type'],
                        "file_size": len(file_content)
                    }
                },
                {
                    "url": f"{base_url}/message/sendMedia",
                    "desc": "媒体发送接口",
                    "headers": {
                        "X-GEWE-TOKEN": self.api_config.get("token")
                    },
                    "file_param": "file",
                    "data": {
                        "appId": self.api_config.get("app_id"),
                        "toWxid": to_wxid,
                        "fileName": file_info['name'],
                        "fileSize": str(len(file_content)),
                        "fileMd5": file_md5
                    }
                }
            ]
            
            # 尝试每个端点
            for endpoint in upload_endpoints:
                try:
                    # 设置较长的超时时间
                    max_retry = 3
                    retry_count = 0
                    
                    while retry_count < max_retry:
                        try:
                            logger.debug(f"[FilePlugin] 尝试使用 {endpoint['desc']} 上传，尝试次数: {retry_count + 1}")
                            
                            # 准备文件数据
                            files = {
                                endpoint["file_param"]: (file_info['name'], file_content, file_info['mime_type'])
                            }
                            
                            # 发送请求
                            response = requests.post(
                                endpoint["url"],
                                headers=endpoint["headers"],
                                data=endpoint["data"],
                                files=files,
                                timeout=60  # 较长超时时间
                            )
                            
                            # 检查响应
                            if response.status_code == 200:
                                result = response.json()
                                # 根据不同API的返回格式检查成功状态
                                success_code = result.get('code', result.get('ret'))
                                
                                if success_code == 200:
                                    logger.info(f"[FilePlugin] {endpoint['desc']} 上传成功: {file_info['name']}")
                                    return True
                                else:
                                    error = result.get('message', result.get('msg', '未知错误'))
                                    logger.warning(f"[FilePlugin] {endpoint['desc']} 上传失败: {error}")
                                    # 尝试下一个端点
                                    break
                            else:
                                logger.warning(f"[FilePlugin] {endpoint['desc']} 请求失败，状态码: {response.status_code}")
                                
                                # 如果服务器错误，可能是临时问题，尝试重试
                                if 500 <= response.status_code < 600:
                                    retry_count += 1
                                    # 指数退避
                                    sleep_time = 2 ** retry_count
                                    time.sleep(sleep_time)
                                    continue
                                else:
                                    # 其他错误，尝试下一个端点
                                    break
                        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                            logger.warning(f"[FilePlugin] {endpoint['desc']} 超时或连接错误: {e}")
                            retry_count += 1
                            if retry_count < max_retry:
                                # 指数退避
                                sleep_time = 2 ** retry_count
                                time.sleep(sleep_time)
                                continue
                            else:
                                logger.error(f"[FilePlugin] {endpoint['desc']} 重试次数超限，尝试下一个端点")
                                break
                        except Exception as e:
                            logger.error(f"[FilePlugin] {endpoint['desc']} 发送异常: {e}")
                            break
                                
                except Exception as e:
                    logger.error(f"[FilePlugin] {endpoint['desc']} 端点异常: {e}")
                    # 继续尝试下一个端点
                    continue
                    
            # 如果所有端点都失败，尝试上传到临时URL，然后发送URL
            try:
                logger.debug("[FilePlugin] 所有直传端点都失败，尝试临时URL方法")
                
                # 上传到临时服务器获取URL
                temp_upload_url = f"{base_url}/file/upload"
                temp_files = {
                    'file': (file_info['name'], file_content, file_info['mime_type'])
                }
                
                temp_response = requests.post(temp_upload_url, files=temp_files, timeout=60)
                if temp_response.status_code == 200:
                    temp_result = temp_response.json()
                    if temp_result.get('code') == 200:
                        file_url = temp_result.get('data', {}).get('url')
                        if file_url:
                            logger.debug(f"[FilePlugin] 临时上传获取URL成功: {file_url}")
                            
                            # 使用URL发送
                            send_url = f"{base_url}/message/sendUrl"
                            
                            payload = {
                                'app_id': self.api_config.get('app_id'),
                                'target_id': to_wxid,
                                'url': file_url,
                                'title': file_info['name'],
                                'describe': f"文件大小: {file_info['size_str']}, 类型: {file_info['mime_type']}"
                            }
                            
                            headers = {
                                'Token': self.api_config.get('token'),
                                'Content-Type': 'application/json'
                            }
                            
                            url_response = requests.post(send_url, headers=headers, json=payload, timeout=10)
                            if url_response.status_code == 200:
                                url_result = url_response.json()
                                if url_result.get('code') == 200:
                                    logger.info(f"[FilePlugin] 通过临时URL发送文件成功: {file_url}")
                                    return True
                                else:
                                    error = url_result.get('message', '未知错误')
                                    logger.error(f"[FilePlugin] 通过临时URL发送文件失败: {error}")
                            else:
                                logger.error(f"[FilePlugin] 临时URL发送请求失败，状态码: {url_response.status_code}")
                    else:
                        logger.error(f"[FilePlugin] 临时上传失败: {temp_result.get('message', '未知错误')}")
                else:
                    logger.error(f"[FilePlugin] 临时上传请求失败，状态码: {temp_response.status_code}")
                    
            except Exception as e:
                logger.error(f"[FilePlugin] 临时URL方法异常: {e}")
            
            # 所有方法都失败
            logger.error(f"[FilePlugin] 所有二进制直传方法都失败")
            return False
            
        except Exception as e:
            logger.error(f"[FilePlugin] 二进制直传失败: {e}")
            logger.error(traceback.format_exc())
            return False

    def _try_upload_with_alternative_api(self, to_wxid: str, file_info: dict) -> bool:
        """使用备用API上传文件
        
        尝试额外的API端点发送文件
        
        Args:
            to_wxid: 接收者ID
            file_info: 文件信息
            
        Returns:
            是否发送成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试使用备用API发送文件: {file_info['name']}")
            
            # 尝试备用API发送文件
            alternative_apis = [
                {
                    "endpoint": "/message/sendFile",
                    "params": {
                        "appId": self.api_config.get("app_id"),
                        "toWxid": to_wxid,
                        "filePath": file_info['path']
                    },
                    "desc": "sendFile接口"
                },
                {
                    "endpoint": "/message/sendFileMsg",
                    "params": {
                        "appId": self.api_config.get("app_id"),
                        "toWxid": to_wxid,
                        "path": file_info['path'],
                        "fileName": file_info['name']
                    },
                    "desc": "sendFileMsg接口"
                },
                {
                    "endpoint": "/message/sendAppFile",
                    "params": {
                        "appId": self.api_config.get("app_id"),
                        "toWxid": to_wxid,
                        "filePath": file_info['path'],
                        "fileName": file_info['name']
                    },
                    "desc": "sendAppFile接口"
                }
            ]
            
            # 依次尝试所有备用API
            for api in alternative_apis:
                try:
                    logger.debug(f"[FilePlugin] 尝试{api['desc']}: {api['endpoint']}")
                    
                    response = requests.post(
                        f"{self.api_config.get('base_url')}{api['endpoint']}",
                        json=api['params'],
                        headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                        timeout=30
                    )
                    
                    # 检查响应
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("ret") == 200:
                            logger.info(f"[FilePlugin] {api['desc']}发送成功: {file_info['name']}")
                            return True
                        else:
                            logger.error(f"[FilePlugin] {api['desc']}返回错误: {result.get('msg')}")
                    else:
                        logger.error(f"[FilePlugin] {api['desc']}请求失败: {response.status_code}")
                except Exception as e:
                    logger.error(f"[FilePlugin] {api['desc']}异常: {e}")
            
            # 如果备用API都失败了，尝试multipart表单上传直传
            try:
                with open(file_info['path'], 'rb') as file_obj:
                    # 使用特殊的名称上传
                    upload_url = f"{self.api_config.get('base_url')}/message/upload"
                    files = {
                        'file': (file_info['name'], file_obj, file_info['mime_type']),
                        'type': (None, 'file')
                    }
                    data = {
                        "appId": self.api_config.get("app_id"),
                        "toWxid": to_wxid
                    }
                    
                    response = requests.post(
                        upload_url,
                        data=data,
                        files=files,
                        headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                        timeout=60
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("ret") == 200:
                            logger.info(f"[FilePlugin] 通用上传接口发送成功: {file_info['name']}")
                            return True
                        else:
                            logger.error(f"[FilePlugin] 通用上传接口返回错误: {result.get('msg')}")
                    else:
                        logger.error(f"[FilePlugin] 通用上传接口请求失败: {response.status_code}")
            except Exception as e:
                logger.error(f"[FilePlugin] 通用上传接口异常: {e}")
            
            return False
            
        except Exception as e:
            logger.error(f"[FilePlugin] 备用API发送失败: {e}")
            return False

    def _try_forward_file(self, to_wxid: str, file_info: dict) -> bool:
        """尝试使用forwardFile接口转发文件
        
        Args:
            to_wxid: 目标微信ID
            file_info: 文件信息
            
        Returns:
            发送是否成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试使用forwardFile接口转发文件: {file_info['name']}")
            
            # 首先检查文件是否存在且可读
            if not os.path.exists(file_info['path']) or not os.path.isfile(file_info['path']):
                logger.error(f"[FilePlugin] 文件不存在或不是常规文件: {file_info['path']}")
                return False
                
            # 尝试读取文件内容，确保文件可读
            try:
                with open(file_info['path'], 'rb') as f:
                    head_content = f.read(100)  # 只读取前100字节
                    if not head_content:
                        logger.error(f"[FilePlugin] 文件内容为空: {file_info['path']}")
                        return False
                    head_md5 = hashlib.md5(head_content).hexdigest()
                    logger.debug(f"[FilePlugin] 文件可读，前100字节哈希: {head_md5}")
            except Exception as e:
                logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                return False
            
            # 计算文件MD5值，用于XML结构
            file_md5 = None
            try:
                with open(file_info['path'], 'rb') as f:
                    file_content = f.read()
                    file_md5 = hashlib.md5(file_content).hexdigest()
                    logger.debug(f"[FilePlugin] 文件MD5: {file_md5}, 大小: {file_info['size_str']}")
            except Exception as e:
                logger.error(f"[FilePlugin] 计算文件MD5失败: {e}")
                return False
            
            # 验证文件URL是否可以访问
            file_url = None
            server_config = self.settings.get("file_server", {})
            
            # 优先使用external_host作为外部访问地址
            external_host = server_config.get("external_host")
            if external_host and self.server_port:
                # 使用external_host构建URL
                file_url = f"http://{external_host}:{self.server_port}/{urllib.parse.quote(os.path.basename(file_info['path']))}"
                
                # 测试URL是否可访问
                try:
                    logger.debug(f"[FilePlugin] 测试文件URL是否可访问: {file_url}")
                    response = requests.head(file_url, timeout=5)
                    if response.status_code == 200:
                        logger.debug(f"[FilePlugin] URL访问测试成功: {file_url}")
                    else:
                        logger.warning(f"[FilePlugin] 通过external_host访问URL失败: {file_url}, 状态码: {response.status_code}")
                        file_url = None  # 重置URL，尝试其他方法
                except Exception as e:
                    logger.warning(f"[FilePlugin] 通过external_host访问URL测试失败: {e}")
                    file_url = None  # 重置URL，尝试其他方法
                    
            # 如果external_host无法访问，回退到常规方法
            if file_url is None:
                # 获取常规URL
                file_url = self._get_file_url(file_info['path'])
                
                # 测试URL是否可访问
                try:
                    logger.debug(f"[FilePlugin] 测试文件URL是否可访问: {file_url}")
                    response = requests.head(file_url, timeout=5)
                    if response.status_code == 200:
                        logger.debug(f"[FilePlugin] URL访问测试成功: {file_url}")
                    else:
                        logger.warning(f"[FilePlugin] URL访问测试失败: {file_url}, 状态码: {response.status_code}")
                        return False
                except Exception as e:
                    logger.error(f"[FilePlugin] URL访问测试异常: {e}")
                    return False
            
            # 生成随机的attachid
            attach_id = f"@cdn_{int(time.time() * 1000)}{os.urandom(5).hex()}"
            
            # 构建XML结构
            xml = f"""<?xml version="1.0"?>
<msg>
    <appmsg appid="" sdkver="0">
        <title>{file_info['name']}</title>
        <des>点击下载文件</des>
        <action></action>
        <type>6</type>
        <showtype>0</showtype>
        <soundtype>0</soundtype>
        <mediatagname></mediatagname>
        <messageext></messageext>
        <messageaction></messageaction>
        <content></content>
        <contentattr>0</contentattr>
        <url>{file_url}</url>
        <lowurl></lowurl>
        <dataurl>{file_url}</dataurl>
        <lowdataurl></lowdataurl>
        <appattach>
            <totallen>{file_info['size']}</totallen>
            <attachid>{attach_id}</attachid>
            <emoticonmd5></emoticonmd5>
            <fileext>{file_info.get('extension', '')}</fileext>
            <cdnthumbaeskey></cdnthumbaeskey>
            <cdnattachurl>{file_url}</cdnattachurl>
            <aeskey>{os.urandom(16).hex()}</aeskey>
            <encryver>0</encryver>
            <filekey>{file_md5}</filekey>
        </appattach>
        <extinfo></extinfo>
        <sourceusername></sourceusername>
        <sourcedisplayname></sourcedisplayname>
        <thumburl></thumburl>
        <md5>{file_md5}</md5>
        <statextstr></statextstr>
    </appmsg>
    <fromusername>{self.api_config.get("app_id")}</fromusername>
    <scene>0</scene>
    <appinfo>
        <version>1</version>
        <appname></appname>
    </appinfo>
    <commenturl></commenturl>
</msg>"""
            
            # 构建请求数据
            data = {
                "appId": self.api_config.get("app_id"),
                "toWxid": to_wxid,
                "filePath": file_info['path'],
                "xml": xml
            }
            
            # 发送请求
            logger.debug(f"[FilePlugin] forwardFile请求数据: {json.dumps(data, ensure_ascii=False)}")
            
            response = requests.post(
                f"{self.api_config.get('base_url')}/message/forwardFile",
                json=data,
                headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                timeout=30
            )
            
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result.get("ret") == 200:
                    logger.info(f"[FilePlugin] 文件转发成功: {file_info['name']}")
                    return True
                else:
                    logger.error(f"[FilePlugin] 文件转发失败: {result.get('msg')}")
            else:
                logger.error(f"[FilePlugin] 文件转发请求失败: {response.status_code}")
            
            return False
        except Exception as e:
            logger.error(f"[FilePlugin] 文件转发异常: {e}")
            logger.error(traceback.format_exc())
            return False

    def _try_post_file(self, to_wxid: str, file_info: dict) -> bool:
        """尝试使用postFile接口发送文件
        
        Args:
            to_wxid: 接收者ID
            file_info: 文件信息
            
        Returns:
            是否发送成功
        """
        try:
            logger.debug(f"[FilePlugin] 尝试使用postFile接口发送文件: {file_info['name']}")
            
            # 检查文件是否存在且可读
            if not os.path.exists(file_info['path']):
                logger.error(f"[FilePlugin] 文件不存在: {file_info['path']}")
                return False
                
            # 尝试读取文件内容，确保完整性
            try:
                with open(file_info['path'], 'rb') as f:
                    file_content = f.read()
                    
                # 验证文件内容
                if len(file_content) == 0:
                    logger.error(f"[FilePlugin] 文件内容为空: {file_info['path']}")
                    return False
                
                file_md5 = hashlib.md5(file_content).hexdigest()
                logger.debug(f"[FilePlugin] 文件MD5: {file_md5}, 大小: {self._format_size(len(file_content))}")
            except Exception as e:
                logger.error(f"[FilePlugin] 读取文件失败: {e}")
                return False
            
            # 确认文件路径的格式
            file_path = os.path.abspath(file_info['path'])
            if os.name == 'nt':  # Windows需要处理路径格式
                file_path = file_path.replace('\\', '/')
            
            # 获取公共访问地址
            server_config = self.settings.get("file_server", {})
            public_host = server_config.get("public_host", "127.0.0.1")
            
            # 确保使用外部可访问的地址
            if public_host == "127.0.0.1" or public_host == "localhost":
                # 尝试获取本机局域网IP地址
                try:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    public_host = s.getsockname()[0]
                    s.close()
                    logger.debug(f"[FilePlugin] 自动获取到本机IP: {public_host}")
                except Exception as e:
                    logger.warning(f"[FilePlugin] 获取本机IP失败: {e}, 使用配置地址")
                    # 使用API地址的主机部分
                    base_url = self.api_config.get("base_url", "")
                    if base_url:
                        try:
                            from urllib.parse import urlparse
                            parsed_url = urlparse(base_url)
                            public_host = parsed_url.netloc.split(':')[0]
                            logger.debug(f"[FilePlugin] 使用API地址的主机部分: {public_host}")
                        except Exception as e:
                            logger.error(f"[FilePlugin] 解析API地址失败: {e}")
            
            # 使用三种文件URL方式尝试发送
            url_methods = [
                # 方法1：使用file://协议
                {
                    "type": "file_protocol",
                    "url": f"file://{file_path}",
                    "desc": "文件协议方式"
                },
                # 方法2：使用文件服务器
                {
                    "type": "file_server",
                    "url": f"http://{public_host}:{self.server_port}/{urllib.parse.quote(os.path.basename(file_info['path']))}",
                    "desc": "文件服务器方式"
                },
                # 方法3：直接使用文件路径
                {
                    "type": "direct_path",
                    "url": file_path,
                    "desc": "直接路径方式"
                }
            ]
            
            # 依次尝试不同的URL方式
            for method in url_methods:
                # 构建请求数据
                data = {
                    "appId": self.api_config.get("app_id"),
                    "toWxid": to_wxid,
                    "fileUrl": method["url"],
                    "fileName": file_info["name"],
                    "fileSize": file_info["size"],
                    "md5": file_md5  # 添加MD5值，帮助接收端校验
                }
                
                logger.debug(f"[FilePlugin] 尝试{method['desc']}发送: {method['url']}")
                
                # 发送请求
                try:
                    response = requests.post(
                        f"{self.api_config.get('base_url')}/message/postFile",
                        json=data,
                        headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                        timeout=30
                    )
                    
                    # 检查响应
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("ret") == 200:
                            logger.info(f"[FilePlugin] {method['desc']}发送成功: {file_info['name']}")
                            return True
                        else:
                            logger.error(f"[FilePlugin] {method['desc']}发送失败: {result.get('msg')}")
                            logger.error(f"[FilePlugin] 完整响应: {json.dumps(result, ensure_ascii=False)}")
                    else:
                        logger.error(f"[FilePlugin] {method['desc']}API请求失败: {response.status_code}")
                        try:
                            logger.error(f"[FilePlugin] 响应内容: {response.text}")
                        except:
                            logger.error("[FilePlugin] 无法读取响应内容")
                except Exception as e:
                    logger.error(f"[FilePlugin] {method['desc']}请求异常: {e}")
            
            # 所有方法都失败了，尝试使用multipart/form-data上传
            logger.debug(f"[FilePlugin] 尝试使用multipart/form-data上传文件")
            try:
                # 构建multipart/form-data请求
                files = {
                    'file': (file_info['name'], open(file_info['path'], 'rb'), file_info['mime_type'])
                }
                
                data = {
                    "appId": self.api_config.get("app_id"),
                    "toWxid": to_wxid
                }
                
                # 发送请求
                response = requests.post(
                    f"{self.api_config.get('base_url')}/message/uploadAndSendFile",
                    data=data,
                    files=files,
                    headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                    timeout=60
                )
                
                # 检查响应
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ret") == 200:
                        logger.info(f"[FilePlugin] 文件上传并发送成功: {file_info['name']}")
                        return True
                    else:
                        logger.error(f"[FilePlugin] 文件上传并发送失败: {result.get('msg')}")
                else:
                    logger.error(f"[FilePlugin] 文件上传API请求失败: {response.status_code}")
            except Exception as e:
                logger.error(f"[FilePlugin] 上传文件异常: {e}")
                
        except Exception as e:
            logger.error(f"[FilePlugin] 发送文件失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            
        return False

    def _try_inline_file(self, to_wxid: str, file_info: dict) -> bool:
        """尝试将文件内容编码后内联在消息中发送
        
        Args:
            to_wxid: 接收者ID
            file_info: 文件信息
            
        Returns:
            是否发送成功
        """
        try:
            # 仅对较小文件使用此方法
            if file_info['size'] > 5 * 1024 * 1024:  # 超过5MB
                logger.debug(f"[FilePlugin] 文件过大，不适合内联发送: {file_info['name']} ({file_info['size_str']})")
                return False
                
            logger.debug(f"[FilePlugin] 尝试内联发送文件: {file_info['name']}")
            
            # 检查文件是否存在且可读
            if not os.path.exists(file_info['path']):
                logger.error(f"[FilePlugin] 文件不存在: {file_info['path']}")
                return False
                
            # 读取文件内容并进行Base64编码
            try:
                with open(file_info['path'], 'rb') as f:
                    file_content = f.read()
                    # 验证文件大小
                    actual_size = len(file_content)
                    if actual_size == 0:
                        logger.error(f"[FilePlugin] 文件内容为空: {file_info['path']}")
                        return False
                    if actual_size != file_info['size']:
                        logger.warning(f"[FilePlugin] 文件大小不匹配: 读取={actual_size}, 元数据={file_info['size']}")
                        file_info['size'] = actual_size
                    
                    # 计算MD5
                    file_md5 = hashlib.md5(file_content).hexdigest()
                    logger.debug(f"[FilePlugin] 文件内容大小: {self._format_size(actual_size)}, MD5: {file_md5}")
                    
                    # Base64编码
                    try:
                        file_base64 = base64.b64encode(file_content).decode('utf-8')
                        logger.debug(f"[FilePlugin] 文件Base64编码成功，编码后大小: {self._format_size(len(file_base64))}")
                    except Exception as e:
                        logger.error(f"[FilePlugin] 文件Base64编码失败: {e}")
                        return False
            except Exception as e:
                logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                return False
            
            # 生成ID值
            current_time = int(time.time() * 1000)
            random_suffix = ''.join(random.choices('0123456789abcdef', k=8))
            attachment_id = f"@cdn_{current_time}{random_suffix}"
            
            # 确定消息类型
            msg_type = "6"  # 默认为文件类型
            if file_info['extension'] in ['jpg', 'jpeg', 'png', 'gif']:
                msg_type = "3"  # 图片类型
            elif file_info['extension'] in ['mp4', 'avi', 'mov']:
                msg_type = "4"  # 视频类型
            
            # 生成CDN URL
            server_config = self.settings.get("file_server", {})
            public_host = server_config.get("public_host", "127.0.0.1")
            
            # 尝试使用IPv4地址
            if ':' in public_host:  # 检查是否是IPv6地址
                logger.warning(f"[FilePlugin] 检测到IPv6地址: {public_host}，尝试使用本地IPv4地址")
                public_host = "127.0.0.1"  # 使用本地回环地址
                
            # 构建相对路径并生成URL
            root_dir = os.path.abspath(self.settings.get("default_dir"))
            try:
                rel_path = os.path.relpath(file_info['path'], root_dir)
                if rel_path.startswith('..'):  # 检查文件是否在根目录外
                    # 复制文件到默认目录
                    default_dir = self.settings.get("default_dir", "./files")
                    target_path = os.path.join(default_dir, os.path.basename(file_info['path']))
                    shutil.copy2(file_info['path'], target_path)
                    file_info['path'] = target_path
                    rel_path = os.path.basename(file_info['path'])
            except ValueError:  # 可能在不同驱动器
                # 复制文件到默认目录
                default_dir = self.settings.get("default_dir", "./files")
                target_path = os.path.join(default_dir, os.path.basename(file_info['path']))
                shutil.copy2(file_info['path'], target_path)
                file_info['path'] = target_path
                rel_path = os.path.basename(file_info['path'])
            
            cdn_url = f"http://{public_host}:{self.server_port}/{urllib.parse.quote(rel_path)}"
            
            # 构建包含文件内容的XML
            xml = f"""<?xml version="1.0"?>
<msg>
    <appmsg appid="" sdkver="0">
        <title>{file_info['name']}</title>
        <des>请下载后查看</des>
        <action></action>
        <type>{msg_type}</type>
        <showtype>0</showtype>
        <soundtype>0</soundtype>
        <mediatagname></mediatagname>
        <messageext></messageext>
        <messageaction></messageaction>
        <content></content>
        <contentattr>0</contentattr>
        <url>{cdn_url}</url>
        <lowurl></lowurl>
        <dataurl>{cdn_url}</dataurl>
        <lowdataurl></lowdataurl>
        <appattach>
            <totallen>{file_info['size']}</totallen>
            <attachid>{attachment_id}</attachid>
            <emoticonmd5></emoticonmd5>
            <fileext>{file_info['extension']}</fileext>
            <cdnthumbaeskey></cdnthumbaeskey>
            <cdnattachurl>{cdn_url}</cdnattachurl>
            <aeskey></aeskey>
            <encryver>0</encryver>
            <filekey>{file_md5}</filekey>
            <filedata>{file_base64}</filedata>
            <fileid>{file_md5}</fileid>
        </appattach>
        <extinfo></extinfo>
        <sourceusername></sourceusername>
        <sourcedisplayname></sourcedisplayname>
        <thumburl></thumburl>
        <md5>{file_md5}</md5>
        <statextstr></statextstr>
    </appmsg>
    <fromusername>{self.api_config.get('app_id')}</fromusername>
    <scene>0</scene>
    <appinfo>
        <version>1</version>
        <appname></appname>
    </appinfo>
    <commenturl></commenturl>
</msg>"""
            
            # 构建发送消息的数据
            data = {
                "appId": self.api_config.get("app_id"),
                "toWxid": to_wxid,
                "xml": xml
            }
            
            # 发送请求
            logger.debug(f"[FilePlugin] 尝试发送内联文件: {file_info['name']}")
            response = requests.post(
                f"{self.api_config.get('base_url')}/message/sendAppMessage",
                json=data,
                headers={"X-GEWE-TOKEN": self.api_config.get("token")},
                timeout=30
            )
            
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result.get("ret") == 200:
                    logger.info(f"[FilePlugin] 内联文件发送成功: {file_info['name']}")
                    return True
                else:
                    logger.error(f"[FilePlugin] 内联文件发送失败: {result.get('msg')}")
                    logger.error(f"[FilePlugin] 完整响应: {json.dumps(result, ensure_ascii=False)}")
            else:
                logger.error(f"[FilePlugin] 内联文件API请求失败: {response.status_code}")
                try:
                    logger.error(f"[FilePlugin] 响应内容: {response.text}")
                except:
                    logger.error("[FilePlugin] 无法读取响应内容")
                
        except Exception as e:
            logger.error(f"[FilePlugin] 内联发送文件失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            
        return False

    def _try_upload_file(self, to_wxid: str, file_info: dict) -> bool:
        """尝试先上传文件再发送
        
        Args:
            to_wxid: 接收者ID
            file_info: 文件信息
            
        Returns:
            是否发送成功
        """
        try:
            # 获取文件服务器URL
            server_config = self.settings.get("file_server", {})
            public_host = server_config.get("public_host", "127.0.0.1")
            port = self.server_port  # 使用实际的服务器端口，而不是配置中的端口
            
            # 计算文件相对路径
            root_dir = os.path.abspath(self.settings.get("default_dir"))
            rel_path = os.path.relpath(file_info['path'], root_dir)
            
            # 构建文件URL，使用相对路径而不是仅文件名
            file_url = f"http://{public_host}:{port}/{urllib.parse.quote(rel_path)}"
            
            logger.debug(f"[FilePlugin] 尝试使用文件URL发送: {file_url}")
            
            # 构建请求数据
            data = {
                "appId": self.api_config.get("app_id"),
                "toWxid": to_wxid,
                "fileUrl": file_url,
                "fileName": file_info["name"]
            }
            
            # 发送请求
            response = requests.post(
                f"{self.api_config.get('base_url')}/message/postFile",
                json=data,
                headers={"X-GEWE-TOKEN": self.api_config.get("token")}
            )
            
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result.get("ret") == 200:
                    logger.info(f"[FilePlugin] 文件上传并发送成功: {file_info['name']}")
                    return True
                else:
                    logger.error(f"[FilePlugin] 文件上传并发送失败: {result.get('msg')}")
            else:
                logger.error(f"[FilePlugin] API请求失败: {response.status_code}, {response.text}")
            
        except Exception as e:
            logger.error(f"[FilePlugin] 上传并发送文件失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            
        return False

    def _manage_shortcut(self, action: str, name: str = None, path: str = None, desc: str = None) -> Tuple[bool, str]:
        """管理快捷方式
        
        Args:
            action: 操作类型（add/delete/list）
            name: 快捷方式名称
            path: 文件路径
            desc: 文件描述
            
        Returns:
            (成功标志, 消息)
        """
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            
            if action == "list":
                # 列出所有快捷方式
                if not self.shortcuts:
                    return True, "当前没有快捷方式"
                    
                result = "可用的快捷方式：\n"
                for name, info in self.shortcuts.items():
                    if isinstance(info, str):
                        result += f"- {name}: {info}\n"
                    else:
                        result += f"- {name}: {info.get('path')} ({info.get('description', '无描述')})\n"
                return True, result.strip()
                
            elif action == "add":
                # 添加快捷方式
                if not name or not path:
                    return False, "请提供快捷方式名称和文件路径"
                    
                # 检查文件是否存在且有效
                file_info = self._get_file_info(path)
                if not file_info.get("exists"):
                    return False, f"文件不存在: {path}"
                if not file_info.get("valid"):
                    return False, file_info.get("error", "无效的文件")
                    
                # 添加快捷方式
                if desc:
                    self.shortcuts[name] = {
                        "path": path,
                        "description": desc,
                        "tags": []
                    }
                else:
                    self.shortcuts[name] = path
                    
                # 保存配置
                with open(config_path, "r+", encoding="utf-8") as f:
                    config = json.load(f)
                    config["shortcuts"] = self.shortcuts
                    f.seek(0)
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    f.truncate()
                    
                return True, f"已添加快捷方式: {name}"
                
            elif action == "delete":
                # 删除快捷方式
                if not name:
                    return False, "请提供要删除的快捷方式名称"
                    
                if name not in self.shortcuts:
                    return False, f"快捷方式不存在: {name}"
                    
                # 删除快捷方式
                del self.shortcuts[name]
                
                # 保存配置
                with open(config_path, "r+", encoding="utf-8") as f:
                    config = json.load(f)
                    config["shortcuts"] = self.shortcuts
                    f.seek(0)
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    f.truncate()
                    
                return True, f"已删除快捷方式: {name}"
                
            else:
                return False, f"不支持的操作: {action}"
                
        except Exception as e:
            logger.error(f"[FilePlugin] 管理快捷方式失败: {e}")
            return False, f"操作失败: {str(e)}"

    def _reload_config(self):
        """重新加载配置文件"""
        try:
            # 获取插件目录
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(plugin_dir, "config.json")
            
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    conf = json.load(f)
                    # 更新配置
                    self.api_config = conf.get("api", {})
                    self.settings = conf.get("settings", {})
                    self.shortcuts = conf.get("shortcuts", {})
                    
                    # 强制覆盖路径设置 - 这是临时调试代码
                    self.settings["default_dir"] = "/app/plugins/file_plugin/files"
                    self.settings["cache_dir"] = "/app/plugins/file_plugin/cache"
                    
                    # 确保文件保留配置存在
                    if "file_retention" not in self.settings:
                        self.settings["file_retention"] = {
                            "default_days": 30,
                            "clean_interval_days": 1,
                            "cache_expire_days": 3
                        }
                    
                    # 强制设置测试文件快捷方式
                    self.shortcuts["测试文件"] = "/app/plugins/file_plugin/files/test.txt"
                    
                    # 确保目录存在
                    os.makedirs(self.settings["default_dir"], exist_ok=True)
                    os.makedirs(self.settings["cache_dir"], exist_ok=True)
                    
                    logger.debug(f"[FilePlugin] 重新加载配置成功: 默认目录={self.settings['default_dir']}, 缓存目录={self.settings['cache_dir']}")
                    return True
            return False
        except Exception as e:
            logger.error(f"[FilePlugin] 重新加载配置失败: {e}")
            return False

    def on_handle_context(self, e_context: EventContext) -> None:
        """处理消息事件
        
        Args:
            e_context: 事件上下文
        """
        try:
            # 重新加载配置
            self._reload_config()
            
            context = e_context["context"]
            if context.type != ContextType.TEXT:
                return
            
            content = context.content
            if not content:
                return
            
            # 打印原始消息内容，用于调试    
            logger.debug(f"[FilePlugin] 收到消息: {content}")
            
            # 获取发送者ID
            from_wxid = None
            
            # 尝试从不同位置获取发送者ID
            # 1. 直接从kwargs获取
            if "from_wxid" in context.kwargs:
                from_wxid = context.kwargs.get("from_wxid")
            
            # 2. 从session_id获取
            elif "session_id" in context.kwargs:
                session_id = context.kwargs.get("session_id")
                # 处理群聊情况 (如 "45027884750@chatroom")
                if "@chatroom" in session_id:
                    from_wxid = session_id
                else:
                    from_wxid = session_id
            
            # 3. 从msg中获取
            elif "msg" in context.kwargs:
                msg = context.kwargs.get("msg")
                if isinstance(msg, dict):
                    # 尝试几种可能的键名
                    for key in ["fromUser", "from_user", "user_id", "userId", "from", "sender"]:
                        if key in msg and msg[key]:
                            from_wxid = msg[key]
                            break
            
            # 4. 尝试从其他可能的键获取
            for key in ["user_id", "userId", "wxid", "sender_id", "senderId", "sender"]:
                if key in context.kwargs and context.kwargs[key]:
                    from_wxid = context.kwargs[key]
                    break
                    
            # 如果仍然没有发送者ID，则使用默认值
            if not from_wxid:
                logger.error("[FilePlugin] 无法获取发送者ID，使用默认值")
                from_wxid = "default_user"  # 使用默认值
            else:
                logger.debug(f"[FilePlugin] 获取到发送者ID: {from_wxid}")

            # 检查是否是帮助命令
            if content.strip() in ["文件", "文件帮助", "文件 帮助", "文件 help"]:
                reply = Reply(ReplyType.TEXT, self.get_help_text())
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            # 检查是否是文件相关命令
            command_prefixes = ["文件 ", "file ", "f ", "📁 ", "发送 ", "传 ", "发文件 ", "传文件 ", 
                              "快捷 ", "快捷方式 ", "链接 ", "文件链接 ", "网址 ", "查找 ", "找 ", 
                              "搜索 ", "搜 ", "文件列表", "文件目录", "快捷列表", "快捷方式列表", 
                              "file list", "fl", "📄 ", "📎 "]
            
            matched_prefix = None
            for prefix in command_prefixes:
                if content.startswith(prefix):
                    matched_prefix = prefix
                    break
                
            if not matched_prefix and not any(content.endswith(f".{ext}") for ext in self.settings.get("allowed_extensions", [])):
                return
            
            # 解析命令
            if matched_prefix:
                content = content[len(matched_prefix):].strip()
                
            logger.debug(f"[FilePlugin] 处理命令: {content}")
            
            # 处理命令
            reply = None
            
            # 检查是否是网络文件URL
            if content.startswith(("http://", "https://", "ftp://")):
                logger.debug(f"[FilePlugin] 检测到网络文件URL: {content}")
                try:
                    # 从URL中提取文件名
                    url_path = urllib.parse.urlparse(content).path
                    file_name = os.path.basename(url_path)
                    if not file_name:
                        file_name = f"download_{int(time.time())}"
                    
                    # 检查文件扩展名
                    file_ext = os.path.splitext(file_name)[1].lower().replace(".", "")
                    if not file_ext or file_ext not in self.settings.get("allowed_extensions", []):
                        # 尝试从Content-Type或Content-Disposition推断扩展名
                        try:
                            head_response = requests.head(content, timeout=5)
                            content_type = head_response.headers.get('content-type', '')
                            if 'pdf' in content_type:
                                file_ext = 'pdf'
                                if not file_name.endswith('.pdf'):
                                    file_name += '.pdf'
                            elif 'image/jpeg' in content_type:
                                file_ext = 'jpg'
                                if not file_name.endswith(('.jpg', '.jpeg')):
                                    file_name += '.jpg'
                            # 可以添加更多类型判断
                        except Exception as e:
                            logger.warning(f"[FilePlugin] 获取文件头信息失败: {e}")
                    
                    logger.debug(f"[FilePlugin] 网络文件名: {file_name}, 类型: {file_ext}")
                    
                    # 下载文件
                    cache_dir = self.settings.get("cache_dir", "./cache")
                    save_path = os.path.join(cache_dir, file_name)
                    
                    # 确保目录存在
                    os.makedirs(cache_dir, exist_ok=True)
                    
                    # 下载文件
                    logger.info(f"[FilePlugin] 开始下载文件: {content}")
                    download_path = self._download_file(content, save_path)
                    
                    if download_path:
                        logger.debug(f"[FilePlugin] 文件下载成功，路径: {download_path}")
                        # 确保下载路径存在
                        if not os.path.exists(download_path):
                            logger.error(f"[FilePlugin] 下载路径不存在: {download_path}")
                            reply = Reply(ReplyType.TEXT, "文件下载后未找到，请稍后重试")
                        else:
                            # 使用绝对路径获取文件信息
                            abs_path = os.path.abspath(download_path)
                            logger.debug(f"[FilePlugin] 文件绝对路径: {abs_path}")
                            file_info = self._get_file_info(abs_path)
                            
                            if file_info.get("exists") and file_info.get("valid"):
                                logger.debug(f"[FilePlugin] 文件有效: {file_info['name']}")
                                if self._send_file(from_wxid, file_info, is_network_file=True):
                                    reply = Reply(ReplyType.TEXT, f"文件下载并发送成功: {file_info['name']} ({file_info['size_str']})")
                                else:
                                    reply = Reply(ReplyType.TEXT, "文件发送失败，请稍后重试")
                            else:
                                error = file_info.get('error', '未知错误')
                                logger.error(f"[FilePlugin] 文件无效: {error}")
                                reply = Reply(ReplyType.TEXT, f"文件无效: {error}")
                    else:
                        reply = Reply(ReplyType.TEXT, "文件下载失败，请检查URL或稍后重试")
                except Exception as e:
                    logger.error(f"[FilePlugin] 处理网络文件失败: {e}")
                    logger.error(traceback.format_exc())
                    reply = Reply(ReplyType.TEXT, f"处理网络文件失败: {str(e)}")
            
            # 如果是直接的文件名
            elif not matched_prefix and content:
                logger.debug(f"[FilePlugin] 尝试作为文件名处理: {content}")
                file_info = self._get_file_info(content)
                if file_info.get("exists") and file_info.get("valid"):
                    if self._send_file(from_wxid, file_info, is_network_file=False):
                        reply = Reply(ReplyType.TEXT, f"文件发送成功: {file_info['name']} ({file_info['size_str']})")
                    else:
                        reply = Reply(ReplyType.TEXT, "文件发送失败，请稍后重试")
                else:
                    error_msg = file_info.get('error', '未知错误')
                    logger.debug(f"[FilePlugin] 文件无效: {error_msg}, 路径: {content}")
                    reply = Reply(ReplyType.TEXT, f"文件无效: {error_msg}")
            
            # 处理其他命令
            else:
                parts = content.split()
                if not parts:
                    reply = Reply(ReplyType.TEXT, self.get_help_text())
                elif parts[0] == "查询" and len(parts) > 1:
                    keyword = parts[1]
                    results = self._search_files(keyword)
                    if not results:
                        reply = Reply(ReplyType.TEXT, f"未找到匹配的文件: {keyword}")
                    else:
                        msg = f"找到 {len(results)} 个匹配的文件:\n"
                        for file_info in results:
                            msg += f"- {file_info['name']} ({file_info['size_str']})\n"
                        reply = Reply(ReplyType.TEXT, msg.strip())
                elif parts[0] == "添加" and len(parts) >= 3:
                    name = parts[1]
                    path = parts[2]
                    desc = " ".join(parts[3:]) if len(parts) > 3 else None
                    success, msg = self._manage_shortcut("add", name, path, desc)
                    reply = Reply(ReplyType.TEXT, msg)
                elif parts[0] == "删除" and len(parts) > 1:
                    name = parts[1]
                    success, msg = self._manage_shortcut("delete", name)
                    reply = Reply(ReplyType.TEXT, msg)
                elif parts[0] == "列表" or content.strip() in ["list", "列表"]:
                    success, msg = self._manage_shortcut("list")
                    reply = Reply(ReplyType.TEXT, msg)
                elif parts[0] == "快捷" and len(parts) > 1:
                    name = parts[1]
                    if name not in self.shortcuts:
                        reply = Reply(ReplyType.TEXT, f"快捷方式不存在: {name}")
                    else:
                        shortcut = self.shortcuts[name]
                        path = shortcut if isinstance(shortcut, str) else shortcut.get("path")
                        file_info = self._get_file_info(path)
                        if not file_info.get("exists") or not file_info.get("valid"):
                            reply = Reply(ReplyType.TEXT, f"文件无效: {file_info.get('error', '未知错误')}")
                        else:
                            if self._send_file(from_wxid, file_info, is_network_file=False):
                                reply = Reply(ReplyType.TEXT, f"文件发送成功: {file_info['name']} ({file_info['size_str']})")
                            else:
                                reply = Reply(ReplyType.TEXT, "文件发送失败，请稍后重试")
                elif parts[0] == "测试":
                    # 处理测试命令
                    if len(parts) > 1:
                        method = parts[1].lower()
                        # 映射方法名
                        method_map = {
                            "内联": "inline", 
                            "转发": "forward", 
                            "上传": "upload", 
                            "本地": "post", 
                            "直接": "direct",
                            "附件": "attachment",
                            "二进制": "binary",
                            "inline": "inline",
                            "forward": "forward",
                            "upload": "upload",
                            "post": "post",
                            "direct": "direct",
                            "attachment": "attachment",
                            "binary": "binary"
                        }
                        method = method_map.get(method)
                        
                        success, msg = self._test_send_file(from_wxid, method)
                        reply = Reply(ReplyType.TEXT, f"测试结果: {msg}")
                    else:
                        # 不指定方法，测试所有方法
                        success, msg = self._test_send_file(from_wxid)
                        reply = Reply(ReplyType.TEXT, f"测试结果: {msg}")
                else:
                    # 尝试作为文件路径处理
                    file_info = self._get_file_info(content)
                    if file_info.get("exists") and file_info.get("valid"):
                        if self._send_file(from_wxid, file_info, is_network_file=False):
                            reply = Reply(ReplyType.TEXT, f"文件发送成功: {file_info['name']} ({file_info['size_str']})")
                        else:
                            reply = Reply(ReplyType.TEXT, "文件发送失败，请稍后重试")
                    else:
                        reply = Reply(ReplyType.TEXT, f"文件无效: {file_info.get('error', '未知错误')}")
            
            # 发送回复
            if reply:
                logger.debug(f"[FilePlugin] 发送回复: {reply.content}")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                
        except Exception as e:
            logger.error(f"[FilePlugin] 处理消息失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            e_context["reply"] = Reply(ReplyType.TEXT, "处理命令时发生错误，请稍后重试")

    def get_help_text(self, **kwargs):
        """获取帮助文本
        
        Args:
            isgroup (bool, optional): 是否为群聊. Defaults to False.
            isadmin (bool, optional): 是否为管理员. Defaults to False.
            verbose (bool, optional): 是否显示详细信息. Defaults to False.
            
        Returns:
            str: 帮助文本
        """
        help_text = """📁 文件插件使用帮助 📁

【基本命令】
文件 example.pdf - 直接发送指定文件
文件 https://example.com/file.pdf - 发送网络文件
文件 查询 关键词 - 搜索文件
文件 列表 - 查看所有快捷方式

【快捷方式管理】
文件 添加 快捷名 文件路径 [描述] - 添加快捷方式
文件 删除 快捷名 - 删除快捷方式
文件 快捷 快捷名 - 使用快捷方式发送文件

【测试命令】
文件 测试 - 生成测试文件并尝试发送
文件 测试内联 - 测试内联发送方法
文件 测试转发 - 测试转发方法
文件 测试上传 - 测试上传方法
文件 测试附件 - 测试附件发送方法（最可靠）
文件 测试二进制 - 测试发送二进制文件

【注意事项】
- 文件大小限制: {0}
- 支持的文件类型: {1}
""".format(
    self._format_size(self.settings.get("max_file_size", 100) * 1024 * 1024),
    ", ".join(self.settings.get("allowed_extensions", []))
)
        return help_text

    def _start_file_cleaner(self):
        """启动文件清理线程"""
        try:
            # 创建文件清理线程
            self.cleaner_thread = threading.Thread(target=self._file_cleaner_task)
            self.cleaner_thread.daemon = True
            self.cleaner_thread.start()
            
            logger.info(f"[FilePlugin] 文件清理线程启动成功")
            
        except Exception as e:
            logger.error(f"[FilePlugin] 启动文件清理线程失败: {e}")
            logger.error(traceback.format_exc())

    def _file_cleaner_task(self):
        """文件清理任务
        
        定期检查和清理过期的缓存文件，同时保留需要长期保存的文件
        """
        try:
            while True:
                try:
                    logger.debug("[FilePlugin] 执行文件清理任务")
                    
                    # 获取清理配置
                    retention_config = self.settings.get("file_retention", {})
                    cache_expire_days = retention_config.get("cache_expire_days", 3)
                    clean_interval_days = retention_config.get("clean_interval_days", 1)
                    
                    # 检查保留文件列表
                    now = datetime.now().timestamp()
                    expired_files = []
                    
                    for file_path, info in list(self.retain_files.items()):
                        # 检查文件是否过期
                        if info["expire_time"] < now:
                            expired_files.append(file_path)
                            del self.retain_files[file_path]
                            logger.debug(f"[FilePlugin] 文件保留期已过期: {file_path}")
                    
                    # 清理缓存目录中的过期文件
                    cache_dir = self.settings.get("cache_dir", "./cache")
                    if os.path.exists(cache_dir):
                        for file_name in os.listdir(cache_dir):
                            file_path = os.path.join(cache_dir, file_name)
                            
                            # 如果不是文件则跳过
                            if not os.path.isfile(file_path):
                                continue
                                
                            # 获取文件修改时间
                            mod_time = os.path.getmtime(file_path)
                            
                            # 如果文件修改时间超过配置的天数且不在保留列表中，则删除
                            if (now - mod_time) > cache_expire_days * 86400 and file_path not in self.retain_files:
                                try:
                                    os.remove(file_path)
                                    logger.debug(f"[FilePlugin] 清理过期缓存文件: {file_path}")
                                except Exception as e:
                                    logger.error(f"[FilePlugin] 删除过期缓存文件失败: {file_path}, {e}")
                    
                except Exception as e:
                    logger.error(f"[FilePlugin] 文件清理任务执行失败: {e}")
                    logger.error(traceback.format_exc())
                
                # 按配置的间隔执行清理
                time.sleep(clean_interval_days * 86400)
                
        except Exception as e:
            logger.error(f"[FilePlugin] 文件清理线程异常: {e}")
            logger.error(traceback.format_exc())

    def _create_test_file(self, file_name="test.txt", content=None):
        """创建测试文件
        
        Args:
            file_name: 文件名
            content: 文件内容，默认为None
            
        Returns:
            文件路径
        """
        try:
            # 确保使用绝对路径
            default_dir = os.path.abspath(self.settings.get("default_dir", "./files"))
            os.makedirs(default_dir, exist_ok=True)
            
            file_path = os.path.join(default_dir, file_name)
            
            # 如果未指定内容，使用默认内容
            if content is None:
                content = f"这是一个测试文件，生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                content += "包含中文内容和ASCII内容\n"
                content += "Hello, world! This is a test file."
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
                
            # 检查文件是否成功创建
            if not os.path.exists(file_path):
                logger.error(f"[FilePlugin] 创建测试文件失败，文件不存在: {file_path}")
                return None
                
            # 输出文件信息
            file_size = os.path.getsize(file_path)
            logger.debug(f"[FilePlugin] 创建测试文件成功: {file_path} ({self._format_size(file_size)})")
            logger.debug(f"[FilePlugin] 文件内容: {content}")
            
            # 也创建一个二进制版本的测试文件，以防文本文件出问题
            bin_file_name = "test_bin.dat"
            bin_file_path = os.path.join(default_dir, bin_file_name)
            
            # 创建一个简单的二进制文件
            try:
                with open(bin_file_path, 'wb') as f:
                    # 写入一些随机二进制数据
                    f.write(os.urandom(1024))  # 1KB的随机数据
                logger.debug(f"[FilePlugin] 创建二进制测试文件成功: {bin_file_path}")
            except Exception as e:
                logger.error(f"[FilePlugin] 创建二进制测试文件失败: {e}")
            
            return file_path
            
        except Exception as e:
            logger.error(f"[FilePlugin] 创建测试文件失败: {e}")
            logger.error(f"[FilePlugin] 错误详情: {traceback.format_exc()}")
            return None
            
    def _test_send_file(self, to_wxid: str, method: str = None):
        """测试发送文件
        
        Args:
            to_wxid: 接收者ID
            method: 发送方法，默认为None (尝试所有方法)
            
        Returns:
            (是否成功, 消息)
        """
        try:
            # 创建测试文件
            file_path = self._create_test_file()
            if not file_path:
                return False, "创建测试文件失败"
            
            # 如果是二进制测试，使用二进制文件
            if method == "binary":
                binary_path = os.path.join(os.path.dirname(file_path), "test_bin.dat")
                if os.path.exists(binary_path):
                    file_path = binary_path
                    method = None  # 重置method，以便尝试所有发送方法
                    logger.debug(f"[FilePlugin] 使用二进制测试文件: {binary_path}")
                else:
                    logger.warning(f"[FilePlugin] 二进制测试文件不存在，使用文本文件: {file_path}")
                
            # 获取文件信息
            file_info = self._get_file_info(file_path)
            if not file_info.get("exists") or not file_info.get("valid"):
                return False, f"文件无效: {file_info.get('error', '未知错误')}"
            
            # 日志输出更多文件信息
            logger.debug(f"[FilePlugin] 测试文件信息: {json.dumps(file_info, ensure_ascii=False)}")
            
            # 检查文件是否真的存在
            if not os.path.exists(file_info['path']):
                logger.error(f"[FilePlugin] 获取文件信息后发现文件不存在: {file_info['path']}")
                # 重新创建文件
                file_path = self._create_test_file("emergency_test.txt", "这是紧急创建的测试文件，因为原文件不存在。")
                if not file_path:
                    return False, "创建紧急测试文件失败"
                file_info = self._get_file_info(file_path)
                
            # 根据指定方法发送
            if method == "inline":
                if self._try_inline_file(to_wxid, file_info):
                    return True, f"内联方法发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "内联方法发送失败"
            elif method == "forward":
                if self._try_forward_file(to_wxid, file_info):
                    return True, f"转发方法发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "转发方法发送失败"
            elif method == "post":
                if self._try_post_file(to_wxid, file_info):
                    return True, f"本地路径发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "本地路径发送失败"
            elif method == "upload":
                if self._try_upload_file(to_wxid, file_info):
                    return True, f"上传方法发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "上传方法发送失败"
            elif method == "direct":
                if self._try_direct_upload(to_wxid, file_info):
                    return True, f"直接上传发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "直接上传发送失败"
            elif method == "attachment":
                if self._try_send_as_attachment(to_wxid, file_info):
                    return True, f"附件方式发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "附件方式发送失败"
            else:
                # 尝试所有方法
                if self._send_file(to_wxid, file_info):
                    return True, f"文件发送成功: {file_info['name']} ({file_info['size_str']})"
                else:
                    return False, "所有发送方法均失败"
            
        except Exception as e:
            logger.error(f"[FilePlugin] 测试发送文件失败: {e}")
            logger.error(traceback.format_exc())
            return False, f"测试失败: {str(e)}" 

    def _verify_file_accessibility(self, file_path: str) -> Tuple[bool, str]:
        """验证文件是否可访问
        
        Args:
            file_path: 文件路径
            
        Returns:
            (是否可访问, 说明消息或URL)
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(file_path):
                logger.error(f"[FilePlugin] 文件不存在: {file_path}")
                return False, "文件不存在"
                
            # 检查文件是否可读
            if not os.access(file_path, os.R_OK):
                logger.error(f"[FilePlugin] 文件不可读: {file_path}")
                return False, "文件不可读"
                
            # 读取文件首部内容，确认不为空
            try:
                with open(file_path, 'rb') as f:
                    head_content = f.read(100)  # 读取前100字节
                    if not head_content:
                        logger.error(f"[FilePlugin] 文件内容为空: {file_path}")
                        return False, "文件内容为空"
                    head_md5 = hashlib.md5(head_content).hexdigest()
                    logger.debug(f"[FilePlugin] 文件可读，首部内容哈希: {head_md5}")
            except Exception as e:
                logger.error(f"[FilePlugin] 读取文件内容失败: {e}")
                return False, f"读取文件内容失败: {e}"
                
            # 检查文件是否在默认目录中，如果不是则复制
            default_dir = os.path.abspath(self.settings.get("default_dir", "./files"))
            if not os.path.abspath(file_path).startswith(default_dir):
                try:
                    # 确保目录存在
                    os.makedirs(default_dir, exist_ok=True)
                    
                    # 复制文件到默认目录
                    new_path = os.path.join(default_dir, os.path.basename(file_path))
                    if os.path.exists(new_path):
                        if os.path.samefile(file_path, new_path):
                            # 同一个文件，不需要复制
                            file_path = new_path
                        else:
                            # 存在同名文件，使用唯一名称
                            filename, ext = os.path.splitext(os.path.basename(file_path))
                            new_path = os.path.join(default_dir, f"{filename}_{int(time.time())}{ext}")
                            shutil.copy2(file_path, new_path)
                            file_path = new_path
                    else:
                        # 直接复制
                        shutil.copy2(file_path, new_path)
                        file_path = new_path
                    
                    logger.debug(f"[FilePlugin] 文件已复制到默认目录: {file_path}")
                except Exception as e:
                    logger.error(f"[FilePlugin] 复制文件到默认目录失败: {e}")
                    # 继续使用原始路径
            
            # 检查文件服务器是否可用
            if not hasattr(self, 'httpd') or self.httpd is None or not hasattr(self, 'server_port'):
                logger.warning("[FilePlugin] 文件服务器未启动，跳过URL可访问性检查")
                return True, "文件内容可访问，但文件服务器未启动"
            
            # 构建URL
            server_config = self.settings.get("file_server", {})
            public_host = server_config.get("public_host", "127.0.0.1")
            file_url = f"http://{public_host}:{self.server_port}/{urllib.parse.quote(os.path.basename(file_path))}"
            
            # 验证URL是否可访问
            try:
                logger.debug(f"[FilePlugin] 验证文件URL是否可访问: {file_url}")
                response = requests.head(file_url, timeout=5)
                
                if response.status_code == 200:
                    # 检查Content-Length
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        file_size = os.path.getsize(file_path)
                        reported_size = int(content_length)
                        
                        if reported_size > 0 and abs(reported_size - file_size) < 100:  # 允许小误差
                            logger.debug(f"[FilePlugin] 文件URL可访问，大小匹配: {file_size} vs {reported_size}")
                            return True, file_url
                        else:
                            logger.warning(f"[FilePlugin] 文件URL可访问，但大小不匹配: {file_size} vs {reported_size}")
                            # 大小不匹配也返回成功，因为某些服务器可能不报告正确的Content-Length
                            return True, file_url
                    else:
                        logger.debug(f"[FilePlugin] 文件URL可访问，但缺少大小信息")
                        return True, file_url
                else:
                    logger.warning(f"[FilePlugin] 文件URL访问失败，状态码: {response.status_code}")
                    
                    # 尝试使用localhost检查服务器是否运行
                    try:
                        local_url = f"http://127.0.0.1:{self.server_port}/{urllib.parse.quote(os.path.basename(file_path))}"
                        local_response = requests.head(local_url, timeout=2)
                        if local_response.status_code == 200:
                            logger.debug("[FilePlugin] 文件服务器本地可访问，但外部不可访问")
                            # 尝试使用备用传输方法，不依赖URL
                            return True, "文件内容可访问，但URL不可外部访问"
                    except Exception as e:
                        logger.error(f"[FilePlugin] 本地URL访问也失败: {e}")
                    
                    # URL访问失败，但文件本身存在且可读，返回成功但提示URL不可访问
                    return True, "文件内容可访问，但URL不可访问"
            except Exception as e:
                logger.error(f"[FilePlugin] 文件服务器访问失败: {e}")
                logger.error(traceback.format_exc())
                
                # 即使URL不可访问，只要文件本身可读，也返回成功
                return True, "文件内容可访问，文件服务器访问测试失败"
            
        except Exception as e:
            logger.error(f"[FilePlugin] 验证文件可访问性失败: {e}")
            logger.error(traceback.format_exc())
            return False, f"验证失败: {e}"