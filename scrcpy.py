from threading import Thread
import subprocess
import socket
import time
import random
from adb_manager import ADBManager

SCRCPY_SERVER_PATH = "scrcpy-server"
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
BASE_PORT = 6666  # 改为基础端口，避免与5555冲突

class Scrcpy:
    def __init__(self):
        self.video_socket = None
        self.audio_socket = None
        self.control_socket = None

        self.android_thread = None
        self.video_thread = None
        self.audio_thread = None
        self.control_thread = None
        self.android_process = None
        
        self.adb_manager = ADBManager()
        self.adb_path = self.adb_manager.adb_path
        self.device_id = None
        self.local_port = None  # 动态分配的本地端口
        
    def find_available_port(self, start_port=BASE_PORT, max_attempts=100):
        """查找可用的端口"""
        for i in range(max_attempts):
            port = start_port + i
            try:
                # 检查端口是否可用
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('127.0.0.1', port))
                    return port
            except OSError:
                continue
        raise Exception(f"无法找到可用端口，尝试了 {max_attempts} 个端口")
        
    def cleanup_adb_forward(self):
        """清理ADB端口转发"""
        if self.local_port:
            try:
                cmd = [self.adb_path]
                if self.device_id:
                    cmd.extend(['-s', self.device_id])
                cmd.extend(["forward", "--remove", f"tcp:{self.local_port}"])
                subprocess.run(cmd, check=False)  # 不抛出异常，因为可能已经被清理
                print(f"Cleaned up ADB forward for port {self.local_port}")
            except Exception as e:
                print(f"Error cleaning up ADB forward: {e}")
            finally:
                self.local_port = None

    def push_server_to_device(self):
        print("Pushing scrcpy-server.jar to device...")
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(['-s', self.device_id])
        cmd.extend(["push", SCRCPY_SERVER_PATH, DEVICE_SERVER_PATH])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error pushing server: {result.stderr}")
            return False
        return True

    def setup_adb_forward(self):
        # 首先清理可能存在的旧转发
        self.cleanup_adb_forward()
        
        # 分配新的可用端口
        self.local_port = self.find_available_port()
        print(f"Setting up ADB forward: tcp:{self.local_port} -> localabstract:scrcpy")
        
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(['-s', self.device_id])
        cmd.extend(["forward", f"tcp:{self.local_port}", "localabstract:scrcpy"])
        
        subprocess.run(cmd, check=True)

    def start_server(self):
        print("Starting scrcpy server in background...")
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(['-s', self.device_id])
        cmd.extend([
            "shell",
            f"CLASSPATH={DEVICE_SERVER_PATH} app_process / com.genymobile.scrcpy.Server 3.1 tunnel_forward=true log_level=VERBOSE video_bit_rate=" + self.video_bit_rate
        ])
        self.android_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while not self.stop:
            stderr_line = self.android_process.stderr.readline().decode().strip()
            if not stderr_line:
                break
            if stderr_line:
                print(f"Server error: {stderr_line}")
        self.android_process.wait()
        print("Server stopped")

    def receive_video_data(self):
        print("Receiving video data (H.264)...")
        try:
            self.video_socket.recv(1)
            while not self.stop:
                try:
                    data = self.video_socket.recv(20480)
                    if not data:
                        break
                    self.video_callback(data)
                except (OSError, ConnectionError, socket.error) as e:
                    if not self.stop:
                        print(f"Video socket error: {e}")
                    break
        except (OSError, ConnectionError, socket.error) as e:
            if not self.stop:
                print(f"Video socket initialization error: {e}")
        print("Video data reception stopped")

    def receive_audio_data(self):
        print("Receiving audio data...")
        try:
            self.audio_socket.recv(1)
            while not self.stop:
                try:
                    data = self.audio_socket.recv(1024)
                    if not data:
                        break
                except (OSError, ConnectionError, socket.error) as e:
                    if not self.stop:
                        print(f"Audio socket error: {e}")
                    break
        except (OSError, ConnectionError, socket.error) as e:
            if not self.stop:
                print(f"Audio socket initialization error: {e}")
        print("Audio data reception stopped")

    def handle_control_conn(self):
        print("Control connection established (idle)...")
        try:
            self.control_socket.recv(1)
            while not self.stop:
                try:
                    data = self.control_socket.recv(1024)
                    if not data:
                        break
                    print("Control Mesg:", data)
                except (OSError, ConnectionError, socket.error) as e:
                    if not self.stop:
                        print(f"Control socket error: {e}")
                    break
        except (OSError, ConnectionError, socket.error) as e:
            if not self.stop:
                print(f"Control socket initialization error: {e}")
        print("Control connection stopped")

    def scrcpy_start(self, video_callback, video_bit_rate):
        self.video_bit_rate = video_bit_rate
        self.video_callback = video_callback
        self.stop = False

        # 检查设备连接状态
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(['-s', self.device_id])
        cmd.append('devices')
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if "device" not in result.stdout:
            print(f"Device {self.device_id} not found or not authorized.")
            return False
        print(f"Device check result: {result.stdout}")

        if not self.push_server_to_device():
            print("Failed to push server files to device.")
            return False

        self.setup_adb_forward()
        self.android_thread = Thread(target=self.start_server, daemon=True)
        self.android_thread.start()
        time.sleep(1)

        try:
            # video connection
            self.video_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.video_socket.connect(('localhost', self.local_port))
            print("Video connection established")

            # audio connection
            self.audio_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.audio_socket.connect(('localhost', self.local_port))
            print("Audio connection established")

            # contorl connection
            self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.control_socket.connect(('localhost', self.local_port))
            print("Control connection established")

            self.video_thread = Thread(target=self.receive_video_data, daemon=True)
            self.audio_thread = Thread(target=self.receive_audio_data, daemon=True)
            self.control_thread = Thread(target=self.handle_control_conn, daemon=True)
            self.video_thread.start()
            self.audio_thread.start()
            self.control_thread.start()
            print("Background tasks started")
            
            return True  # 成功启动
            
        except Exception as e:
            print(f"Error establishing connections: {e}")
            self.scrcpy_stop()  # 清理资源
            return False

    def scrcpy_stop(self):
        print("Stopping Scrcpy")
        self.stop = True
        
        # 安全地关闭socket连接
        sockets_to_close = [
            ('video_socket', self.video_socket),
            ('audio_socket', self.audio_socket),
            ('control_socket', self.control_socket)
        ]
        
        for socket_name, sock in sockets_to_close:
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except (OSError, socket.error):
                    pass  # 套接字可能已经关闭
                try:
                    sock.close()
                except (OSError, socket.error):
                    pass

        # 等待线程结束
        threads_to_join = [
            ('video_thread', self.video_thread),
            ('audio_thread', self.audio_thread),
            ('control_thread', self.control_thread)
        ]
        
        for thread_name, thread in threads_to_join:
            if thread and thread.is_alive():
                try:
                    thread.join(timeout=3)
                    if thread.is_alive():
                        print(f"Warning: {thread_name} did not stop within timeout")
                except Exception as e:
                    print(f"Error joining {thread_name}: {e}")
            
        # 终止Android进程
        if self.android_process:
            try:
                self.android_process.terminate()
                # 给进程一些时间优雅退出
                try:
                    self.android_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    print("Force killing Android process")
                    self.android_process.kill()
            except Exception as e:
                print(f"Error terminating Android process: {e}")
                
        if self.android_thread and self.android_thread.is_alive():
            try:
                self.android_thread.join(timeout=3)
                if self.android_thread.is_alive():
                    print("Warning: android_thread did not stop within timeout")
            except Exception as e:
                print(f"Error joining android_thread: {e}")
            
        # 清理ADB端口转发
        try:
            self.cleanup_adb_forward()
        except Exception as e:
            print(f"Error cleaning up ADB forward: {e}")
        
        print("Scrcpy stopped")

    def scrcpy_send_control(self, data):
        try:
            if not hasattr(self, 'control_socket') or self.control_socket is None:
                print("Error: Control socket not initialized")
                return False
            
            # 检查套接字是否仍然连接
            try:
                # 尝试发送数据
                self.control_socket.send(data)
                print(f"Control data sent successfully: {len(data)} bytes")
                return True
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                print(f"Control socket connection lost: {e}")
                return False
            except Exception as e:
                print(f"Error sending control data: {e}")
                return False
                
        except Exception as e:
            print(f"Unexpected error in scrcpy_send_control: {e}")
            return False