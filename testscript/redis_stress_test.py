import socket
import struct
import threading
import time

# ================= 压测参数配置 =================
SERVER_IP = '127.0.0.1'
SERVER_PORT = 8080
CONCURRENCY = 200         # 为了看清防线效果，并发先改为200
REQUESTS_PER_CLIENT = 500 # 每个并发发送 500 个包
# ================================================

successful_requests = 0
failed_requests_by_blacklist = 0 # 被黑名单拦截断开的次数
other_errors = 0
total_time = 0

def create_ping_packet():
    return struct.pack('!HHi', 8, 0, 0)

def client_task():
    global successful_requests, failed_requests_by_blacklist, other_errors
    ping_packet = create_ping_packet()
    
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) 
        client_socket.connect((SERVER_IP, SERVER_PORT))
        
        for _ in range(REQUESTS_PER_CLIENT):
            try:
                client_socket.sendall(ping_packet)
                response = client_socket.recv(8)
                if len(response) == 8:
                    successful_requests += 1
                else:
                    # 返回空数据，说明被服务器极速 Close 了
                    failed_requests_by_blacklist += 1
                    break 
            except (ConnectionResetError, BrokenPipeError):
                # TCP RST 拔网线异常，说明 L1 拦截生效
                failed_requests_by_blacklist += 1
                break
            except Exception:
                other_errors += 1
                break
                
        client_socket.close()
    except Exception:
        other_errors += 1

def run_stress_test():
    global total_time
    print(f"[*] 目标: {SERVER_IP}:{SERVER_PORT} | 并发: {CONCURRENCY} | 总请求预估: {CONCURRENCY * REQUESTS_PER_CLIENT}")
    print("[*] 正在发动 CC 攻击，请观察服务器表现...")
    
    threads = []
    start_time = time.time()
    
    for _ in range(CONCURRENCY):
        t = threading.Thread(target=client_task)
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    total_time = time.time() - start_time
    
    print("\n================ CC 拦截验证报告 ================")
    print(f"总耗时:      {total_time:.2f} 秒")
    print(f"✅ 成功放行: {successful_requests} 次 (应在 20 次左右触发阈值，加上多线程并发抢占，可能在数十次)")
    print(f"🛡️ 成功拦截: {failed_requests_by_blacklist} 次 (被 L1 极速拔网线的恶意请求)")
    print(f"❌ 其他错误: {other_errors} 次")
    print("=================================================")

if __name__ == '__main__':
    run_stress_test()
