

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>    //uintptr_t
#include <stdarg.h>    //va_start....
#include <unistd.h>    //STDERR_FILENO等
#include <sys/time.h>  //gettimeofday
#include <time.h>      //localtime_r
#include <fcntl.h>     //open
#include <errno.h>     //errno
#include <sys/socket.h>
#include <sys/ioctl.h> //ioctl
#include <arpa/inet.h>
#include <pthread.h>   //多线程
#include <memory>
#include <chrono>

#include "ngx_c_conf.h"
#include "ngx_macro.h"
#include "ngx_global.h"
#include "ngx_func.h"
#include "ngx_c_socket.h"
#include "ngx_c_memory.h"
#include "ngx_c_crc32.h"
#include "ngx_c_slogic.h"  
#include "ngx_logiccomm.h"  
#include "ngx_c_lockmutex.h"
#include "ngx_c_mysql_connpool.h"
#include "ngx_c_mysql_dao.h"
#include "ngx_c_user_cache.h"

//定义成员函数指针
typedef bool (CLogicSocket::*handler)(  lpngx_connection_t pConn,      //连接池中连接的指针
                                        LPSTRUC_MSG_HEADER pMsgHeader,  //消息头指针
                                        char *pPkgBody,                 //包体指针
                                        unsigned short iBodyLength);    //包体长度

//用来保存 成员函数指针 的这么个数组
static const handler statusHandler[] = 
{
    //数组前5个元素，保留，以备将来增加一些基本服务器功能
    &CLogicSocket::_HandlePing,                             //【0】：心跳包的实现
    NULL,                                                   //【1】：下标从0开始
    NULL,                                                   //【2】：下标从0开始
    NULL,                                                   //【3】：下标从0开始
    NULL,                                                   //【4】：下标从0开始
 
    //开始处理具体的业务逻辑
    &CLogicSocket::_HandleRegister,                         //【5】：实现具体的注册功能
    &CLogicSocket::_HandleLogIn,                            //【6】：实现具体的登录功能
    &CLogicSocket::_HandleGetUserInfo,                       //【7】：Cache-Aside 查询用户信息
    //......其他待扩展，比如实现攻击功能，实现加血功能等等；


};

static int64_t ngx_ntoh64(int64_t n)
{
    uint64_t v = 0;
    memcpy(&v, &n, sizeof(v));
    uint64_t r = ((v & 0xFF00000000000000ULL) >> 56) |
                 ((v & 0x00FF000000000000ULL) >> 40) |
                 ((v & 0x0000FF0000000000ULL) >> 24) |
                 ((v & 0x000000FF00000000ULL) >> 8)  |
                 ((v & 0x00000000FF000000ULL) << 8)  |
                 ((v & 0x0000000000FF0000ULL) << 24) |
                 ((v & 0x000000000000FF00ULL) << 40) |
                 ((v & 0x00000000000000FFULL) << 56);
    int64_t out = 0;
    memcpy(&out, &r, sizeof(out));
    return out;
}

static int64_t ngx_hton64(int64_t n)
{
    return ngx_ntoh64(n);
}

void CLogicSocket::SendGetUserInfoResponse(LPSTRUC_MSG_HEADER pMsgHeader,
                                    int iResult,
                                    int64_t userId,
                                    const char *username)
{
    CMemory *p_memory = CMemory::GetInstance();
    CCRC32 *p_crc32 = CCRC32::GetInstance();
    const int iSendLen = sizeof(STRUCT_GET_USER_INFO_RESP);

    char *p_sendbuf = (char *)p_memory->AllocMemory(m_iLenMsgHeader + m_iLenPkgHeader + iSendLen, false);
    memcpy(p_sendbuf, pMsgHeader, m_iLenMsgHeader);

    LPCOMM_PKG_HEADER pPkgHeader = (LPCOMM_PKG_HEADER)(p_sendbuf + m_iLenMsgHeader);
    pPkgHeader->msgCode = _CMD_GET_USER_INFO;
    pPkgHeader->msgCode = htons(pPkgHeader->msgCode);
    pPkgHeader->pkgLen = htons(m_iLenPkgHeader + iSendLen);

    LPSTRUCT_GET_USER_INFO_RESP p_sendInfo =
        (LPSTRUCT_GET_USER_INFO_RESP)(p_sendbuf + m_iLenMsgHeader + m_iLenPkgHeader);
    memset(p_sendInfo, 0, iSendLen);
    p_sendInfo->iResult = htonl(iResult);
    p_sendInfo->userId = ngx_hton64(userId);
    if (username) {
        strncpy(p_sendInfo->username, username, sizeof(p_sendInfo->username) - 1);
    }

    pPkgHeader->crc32 = p_crc32->Get_CRC((unsigned char *)p_sendInfo, iSendLen);
    pPkgHeader->crc32 = htonl(pPkgHeader->crc32);
    msgSend(p_sendbuf);
}
#define AUTH_TOTAL_COMMANDS sizeof(statusHandler)/sizeof(handler) //整个命令有多少个，编译时即可知道

//构造函数
CLogicSocket::CLogicSocket()
{

}
//析构函数
CLogicSocket::~CLogicSocket()
{
    if(m_pRedis){
        delete m_pRedis;
        m_pRedis = nullptr;
    }
}

//初始化函数【fork()子进程之前干这个事】
//成功返回true，失败返回false
bool CLogicSocket::Initialize()
{
    return CSocekt::Initialize();  // Master 只初始化监听资源
}

//仅在 fork 后的 Worker 中、启动线程前调用，避免继承 Redis 连接池。
bool CLogicSocket::InitializeRedis()
{
    CConfig *p_config = CConfig::GetInstance();
    m_redisRateLimitEnabled = p_config->GetIntDefault("RedisRateLimitEnable", 1) != 0;
    m_redisRateLimitWindowSec = p_config->GetIntDefault("RedisRateLimitWindowSec", 60);
    m_redisRateLimitMaxRequests = p_config->GetIntDefault("RedisRateLimitMaxRequests", 20);
    if (m_redisRateLimitWindowSec <= 0)
        m_redisRateLimitWindowSec = 60;
    if (m_redisRateLimitMaxRequests <= 0)
        m_redisRateLimitMaxRequests = 20;

    try{
        sw::redis::ConnectionOptions conn_opts;
        conn_opts.host = "127.0.0.1";
        conn_opts.port = 6379;
        conn_opts.connect_timeout = std::chrono::milliseconds(5000);
        conn_opts.socket_timeout = std::chrono::milliseconds(5000);
        //conn_opts.password = "yourpassword"; // 如果Redis设置了密码，取消注释并设置密码

        sw::redis::ConnectionPoolOptions pool_opts;
        pool_opts.wait_timeout = std::chrono::milliseconds(5000);
        pool_opts.size = 10; // 连接池大小，根据需要调整

        m_pRedis = new sw::redis::Redis(conn_opts, pool_opts);
    }catch (const sw::redis::Error &e) {
        ngx_log_stderr(0,"CLogicSocket::InitializeRedis()中创建Redis连接池失败: %s", e.what());
        return false;
    }

    ngx_log_error_core(NGX_LOG_NOTICE,0,
        "Worker %P Redis连接池创建成功，限流=%s，窗口=%d秒，阈值=%d",
        ngx_pid, m_redisRateLimitEnabled ? "on" : "off",
        m_redisRateLimitWindowSec, m_redisRateLimitMaxRequests);
    return true;
}

void CLogicSocket::Shutdown_subproc()
{
    CSocekt::Shutdown_subproc(); // all users of Redis have joined
    delete m_pRedis;
    m_pRedis = nullptr;
}

//业务线程分发入口处理
void CLogicSocket::threadRecvProcFunc(char *pMsgBuf)
{          
    LPSTRUC_MSG_HEADER pMsgHeader = (LPSTRUC_MSG_HEADER)pMsgBuf;                  //消息头
    LPCOMM_PKG_HEADER  pPkgHeader = (LPCOMM_PKG_HEADER)(pMsgBuf+m_iLenMsgHeader); //包头
    void  *pPkgBody;                                                              //指向包体的指针
    unsigned short pkglen = ntohs(pPkgHeader->pkgLen);                            //客户端指明的包宽度【包头+包体】
   // lpngx_connection_t p_Conn = pMsgHeader->pConn;

    if(m_iLenPkgHeader == pkglen)
    {
        //没有包体，只有包头
		if(pPkgHeader->crc32 != 0) //只有包头的crc值给0
		{
			return; //crc错，直接丢弃
		}
		pPkgBody = NULL;
    }
    else 
	{
        //有包体，走到这里
		pPkgHeader->crc32 = ntohl(pPkgHeader->crc32);		          //针对4字节的数据，网络序转主机序
		pPkgBody = (void *)(pMsgBuf+m_iLenMsgHeader+m_iLenPkgHeader); //跳过消息头 以及 包头 ，指向包体

		int calccrc = CCRC32::GetInstance()->Get_CRC((unsigned char *)pPkgBody,pkglen-m_iLenPkgHeader); //计算纯包体的crc值
		if(calccrc != pPkgHeader->crc32) //服务器端根据包体计算crc值，和客户端传递过来的包头中的crc32信息比较
		{
            ngx_log_stderr(0,"CLogicSocket::threadRecvProcFunc()中CRC错误[服务器:%d/客户端:%d]，丢弃数据!",calccrc,pPkgHeader->crc32);    //正式代码中可以干掉这个信息
			return; //crc错，直接丢弃
		}
        else
        {
            //ngx_log_stderr(0,"CLogicSocket::threadRecvProcFunc()中CRC正确[服务器:%d/客户端:%d]，不错!",calccrc,pPkgHeader->crc32);
        }        
	}
    //在上面出现过一遍
    unsigned short imsgCode = ntohs(pPkgHeader->msgCode); //消息代码拿出来
    lpngx_connection_t p_Conn = pMsgHeader->pConn;        //消息头中藏着连接池中连接的指针

    struct sockaddr peer;
    {
        std::lock_guard<std::recursive_mutex> lock(m_ioMutex);
        if (p_Conn->fd == -1 || p_Conn->iCurrsequence != pMsgHeader->iCurrsequence)
            return;
        ++p_Conn->activeJobs;
        peer = p_Conn->s_sockaddr;
    }
    struct JobGuard {
        lpngx_connection_t conn;
        ~JobGuard() { --conn->activeJobs; }
    } job{p_Conn}; // recycle cannot reuse this object until business code returns

    //第二步：新增L2拦截层，利用Redis lua脚本进行全局限流
    //只有合法的包，才值得Redis去查一下是不是他在恶意攻击

    if(m_pRedis !=nullptr && m_redisRateLimitEnabled){
    u_char ip_text[100]={0};
    ngx_sock_ntop(&peer, 0, ip_text, sizeof(ip_text));
    std::string client_ip((const char*)ip_text);
    try
    {
        //Lua限流脚本：利用自增特性原子操作，第一次自增时赋予过期时间60秒
        //逻辑：将键值+1。如果加完后是1（说明是新来的），则给它设置过期时间。最后返回计数值
        std::string lua_script = R"(
          local current = redis.call('INCR',KEYS[1])
            if current == 1 then
                redis.call('EXPIRE',KEYS[1],tonumber(ARGV[1]))
            end
            return current
    )";
    std::string redis_key = "RateLimit:" + client_ip;

    //执行Luae脚本，（传一个Key：IP,传一个Arg：60秒过期时间）
    long long current_count = m_pRedis->eval<long long>(
        lua_script,
        {redis_key},//KEYS数组
        {std::to_string(m_redisRateLimitWindowSec)}//ARGV数组
    );
    //核心判断：同一IP在配置窗口内发包超过阈值，就认为是恶意攻击。
    //在测试QPS时放开限制，正式环境根据实际情况调整这个阈值
    if(current_count > m_redisRateLimitMaxRequests){
        ngx_log_stderr(0,"[防CC攻击]L2 Redis Lua限流触发，IP: %s, 窗口内请求数: %L",
                       client_ip.c_str(), (int64_t)current_count);
        //联动L1防线：跨层封杀，封禁时长与限流窗口一致。
        AddIpToBlacklist(client_ip, m_redisRateLimitWindowSec);
        return; //丢弃这个包，不处理了
      }
    }

    catch(const sw::redis::Error &e){
        //Redis如果挂了，或者网络抖动，为了不让整个服务器瘫痪，我们仅打印日志，放行请求（降级处理）
        ngx_log_stderr(0,"L2 Redis Lua 限流异常（服务降级放行）： %s", e.what());
        }
    }

    //第三步：通过了重重防线，正常处理业务逻辑
	if(imsgCode >= AUTH_TOTAL_COMMANDS) //无符号数不可能<0
    {
        ngx_log_stderr(0,"CLogicSocket::threadRecvProcFunc()中imsgCode=%d消息码不对!",imsgCode); //这种有恶意倾向或者错误倾向的包，希望打印出来看看是谁干的
        return; //丢弃不理这种包【恶意包或者错误包】
    }

    if(statusHandler[imsgCode] == NULL) //这种用imsgCode的方式可以使查找要执行的成员函数效率特别高
    {
        ngx_log_stderr(0,"CLogicSocket::threadRecvProcFunc()中imsgCode=%d消息码找不到对应的处理函数!",imsgCode); //这种有恶意倾向或者错误倾向的包，希望打印出来看看是谁干的
        return;  //没有相关的处理函数
    }
    //分发到具体的业务函数中
    (this->*statusHandler[imsgCode])(p_Conn,pMsgHeader,(char *)pPkgBody,pkglen-m_iLenPkgHeader);
    return;	
}

//心跳包检测时间到，该去检测心跳包是否超时的事宜，本函数是子类函数，实现具体的判断动作
void CLogicSocket::procPingTimeOutChecking(LPSTRUC_MSG_HEADER tmpmsg,time_t cur_time)
{
    std::lock_guard<std::recursive_mutex> lock(m_ioMutex);
    if (m_draining) {
        CMemory::GetInstance()->FreeMemory(tmpmsg);
        return;
    }
    CMemory *p_memory = CMemory::GetInstance();

    if(tmpmsg->iCurrsequence == tmpmsg->pConn->iCurrsequence) //此连接没断
    {
        lpngx_connection_t p_Conn = tmpmsg->pConn;

        if(/*m_ifkickTimeCount == 1 && */m_ifTimeOutKick == 1)  //能调用到本函数第一个条件肯定成立，所以第一个条件加不加无所谓，主要是第二个条件
        {
            zdClosesocketProc(p_Conn); 
        }            
        else if( (cur_time - p_Conn->lastPingTime ) > (m_iWaitTime*3+10) ) //超时踢的判断标准就是 每次检查的时间间隔*3，超过这个时间没发送心跳包，就踢【大家可以根据实际情况自由设定】
        {
            //踢出去【如果此时此刻该用户正好断线，则这个socket可能立即被后续上来的连接复用  如果真有人这么倒霉，赶上这个点了，那么可能错踢，错踢就错踢】            
            //ngx_log_stderr(0,"时间到不发心跳包，踢出去!");   //感觉OK
            zdClosesocketProc(p_Conn); 
        }   
             
        p_memory->FreeMemory(tmpmsg);//内存要释放
    }
    else //此连接断了
    {
        p_memory->FreeMemory(tmpmsg);//内存要释放
    }
    return;
}

//发送没有包体的数据包给客户端
void CLogicSocket::SendNoBodyPkgToClient(LPSTRUC_MSG_HEADER pMsgHeader,unsigned short iMsgCode)
{
    CMemory  *p_memory = CMemory::GetInstance();

    char *p_sendbuf = (char *)p_memory->AllocMemory(m_iLenMsgHeader+m_iLenPkgHeader,false);
    char *p_tmpbuf = p_sendbuf;
    
	memcpy(p_tmpbuf,pMsgHeader,m_iLenMsgHeader);
	p_tmpbuf += m_iLenMsgHeader;

    LPCOMM_PKG_HEADER pPkgHeader = (LPCOMM_PKG_HEADER)p_tmpbuf;	  //指向的是我要发送出去的包的包头	
    pPkgHeader->msgCode = htons(iMsgCode);	
    pPkgHeader->pkgLen = htons(m_iLenPkgHeader); 
	pPkgHeader->crc32 = 0;		
    msgSend(p_sendbuf);
    return;
}

//----------------------------------------------------------------------------------------------------------
//处理各种业务逻辑
bool CLogicSocket::_HandleRegister(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength)
{
    //ngx_log_stderr(0,"执行了CLogicSocket::_HandleRegister()!");
    
    //(1)首先判断包体的合法性
    if(pPkgBody == NULL) //具体看客户端服务器约定，如果约定这个命令[msgCode]必须带包体，那么如果不带包体，就认为是恶意包，直接不处理    
    {        
        return false;
    }
		    
    int iRecvLen = sizeof(STRUCT_REGISTER); 
    if(iRecvLen != iBodyLength) //发送过来的结构大小不对，认为是恶意包，直接不处理
    {     
        return false; 
    }

  
    CLock lock(&pConn->logicPorcMutex); //凡是和本用户有关的访问都互斥
    
    //(3)取得了整个发送过来的数据
    LPSTRUCT_REGISTER p_RecvInfo = (LPSTRUCT_REGISTER)pPkgBody; 
    p_RecvInfo->iType = ntohl(p_RecvInfo->iType);          //所有数值型,short,int,long,uint64_t,int64_t这种大家都不要忘记传输之前主机网络序，收到后网络转主机序
    p_RecvInfo->username[sizeof(p_RecvInfo->username)-1]=0;//这非常关键，防止客户端发送过来畸形包，导致服务器直接使用这个数据出现错误。 
    p_RecvInfo->password[sizeof(p_RecvInfo->password)-1]=0;//这非常关键，防止客户端发送过来畸形包，导致服务器直接使用这个数据出现错误。 


    int dbResult = NGX_DB_ERR_POOL;
    std::shared_ptr<MYSQL> mysql_conn = CMysqlConnPool::GetInstance()->GetConnection();
    if (mysql_conn) {
        dbResult = CMysqlDao::RegisterUser(mysql_conn.get(),
                                           p_RecvInfo->username,
                                           p_RecvInfo->password);
    } else {
        ngx_log_stderr(0, "_HandleRegister: 获取 MySQL 连接失败");
    }

	LPCOMM_PKG_HEADER pPkgHeader;	
	CMemory  *p_memory = CMemory::GetInstance();
	CCRC32   *p_crc32 = CCRC32::GetInstance();
    int iSendLen = sizeof(STRUCT_REGISTER);  

    char *p_sendbuf = (char *)p_memory->AllocMemory(m_iLenMsgHeader+m_iLenPkgHeader+iSendLen,false);
    memcpy(p_sendbuf,pMsgHeader,m_iLenMsgHeader);
    pPkgHeader = (LPCOMM_PKG_HEADER)(p_sendbuf+m_iLenMsgHeader);
    pPkgHeader->msgCode = _CMD_REGISTER;
    pPkgHeader->msgCode = htons(pPkgHeader->msgCode);
    pPkgHeader->pkgLen  = htons(m_iLenPkgHeader + iSendLen);
    LPSTRUCT_REGISTER p_sendInfo = (LPSTRUCT_REGISTER)(p_sendbuf+m_iLenMsgHeader+m_iLenPkgHeader);
    memset(p_sendInfo, 0, iSendLen);
    p_sendInfo->iType = htonl(dbResult);
    strncpy(p_sendInfo->username, p_RecvInfo->username, sizeof(p_sendInfo->username) - 1);
    strncpy(p_sendInfo->password, p_RecvInfo->password, sizeof(p_sendInfo->password) - 1);

    pPkgHeader->crc32   = p_crc32->Get_CRC((unsigned char *)p_sendInfo,iSendLen);
    pPkgHeader->crc32   = htonl(pPkgHeader->crc32);
    msgSend(p_sendbuf);

    return (dbResult == NGX_DB_OK);
}
bool CLogicSocket::_HandleLogIn(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength)
{    
    if(pPkgBody == NULL)
    {        
        return false;
    }		    
    int iRecvLen = sizeof(STRUCT_LOGIN); 
    if(iRecvLen != iBodyLength) 
    {     
        return false; 
    }
    CLock lock(&pConn->logicPorcMutex);
        
    LPSTRUCT_LOGIN p_RecvInfo = (LPSTRUCT_LOGIN)pPkgBody;
    p_RecvInfo->iResult = ntohl(p_RecvInfo->iResult);
    p_RecvInfo->username[sizeof(p_RecvInfo->username)-1]=0;
    p_RecvInfo->password[sizeof(p_RecvInfo->password)-1]=0;

    int dbResult = NGX_DB_ERR_POOL;
    std::shared_ptr<MYSQL> mysql_conn = CMysqlConnPool::GetInstance()->GetConnection();
    if (mysql_conn) {
        dbResult = CMysqlDao::VerifyLogin(mysql_conn.get(),
                                          p_RecvInfo->username,
                                          p_RecvInfo->password);
        if (dbResult != NGX_DB_OK) {
            dbResult = NGX_DB_ERR_FAILED;
        }
    } else {
        ngx_log_stderr(0, "_HandleLogIn: 获取 MySQL 连接失败");
    }

	LPCOMM_PKG_HEADER pPkgHeader;	
	CMemory  *p_memory = CMemory::GetInstance();
	CCRC32   *p_crc32 = CCRC32::GetInstance();

    int iSendLen = sizeof(STRUCT_LOGIN);  
    char *p_sendbuf = (char *)p_memory->AllocMemory(m_iLenMsgHeader+m_iLenPkgHeader+iSendLen,false);    
    memcpy(p_sendbuf,pMsgHeader,m_iLenMsgHeader);    
    pPkgHeader = (LPCOMM_PKG_HEADER)(p_sendbuf+m_iLenMsgHeader);
    pPkgHeader->msgCode = _CMD_LOGIN;
    pPkgHeader->msgCode = htons(pPkgHeader->msgCode);
    pPkgHeader->pkgLen  = htons(m_iLenPkgHeader + iSendLen);    
    LPSTRUCT_LOGIN p_sendInfo = (LPSTRUCT_LOGIN)(p_sendbuf+m_iLenMsgHeader+m_iLenPkgHeader);
    memset(p_sendInfo, 0, iSendLen);
    p_sendInfo->iResult = htonl(dbResult);
    strncpy(p_sendInfo->username, p_RecvInfo->username, sizeof(p_sendInfo->username) - 1);
    pPkgHeader->crc32   = p_crc32->Get_CRC((unsigned char *)p_sendInfo,iSendLen);
    pPkgHeader->crc32   = htonl(pPkgHeader->crc32);
    msgSend(p_sendbuf);
    return (dbResult == NGX_DB_OK);
}

bool CLogicSocket::_HandleGetUserInfo(lpngx_connection_t pConn, LPSTRUC_MSG_HEADER pMsgHeader,
                                      char *pPkgBody, unsigned short iBodyLength)
{
    if (pPkgBody == NULL) {
        return false;
    }
    if (static_cast<unsigned short>(sizeof(STRUCT_GET_USER_INFO_REQ)) != iBodyLength) {
        return false;
    }

    CLock lock(&pConn->logicPorcMutex);

    LPSTRUCT_GET_USER_INFO_REQ p_recv = (LPSTRUCT_GET_USER_INFO_REQ)pPkgBody;
    int64_t userId = ngx_ntoh64(p_recv->userId);
    if (userId <= 0) {
        SendGetUserInfoResponse(pMsgHeader, NGX_USER_ERR_BADREQ, 0, nullptr);
        return false;
    }

    UserInfoDto cached{};
    bool isNullCached = false;
    if (m_pRedis != nullptr &&
        CUserCacheService::TryGetFromCache(m_pRedis, userId, cached, &isNullCached)) {
        SendGetUserInfoResponse(pMsgHeader, NGX_USER_OK, cached.id, cached.username);
        return true;
    }
    if (isNullCached) {
        SendGetUserInfoResponse(pMsgHeader, NGX_USER_NOT_FOUND, userId, nullptr);
        return false;
    }

    std::shared_ptr<MYSQL> mysql_conn = CMysqlConnPool::GetInstance()->GetConnection();
    if (!mysql_conn) {
        ngx_log_stderr(0, "_HandleGetUserInfo: 获取 MySQL 连接失败");
        SendGetUserInfoResponse(pMsgHeader, NGX_USER_ERR_BACKEND, userId, nullptr);
        return false;
    }

    UserInfoDto dbUser{};
    int dbResult = CMysqlDao::GetUserById(mysql_conn.get(), userId, dbUser);
    if (dbResult == NGX_DB_OK) {
        if (m_pRedis != nullptr) {
            CUserCacheService::SetUserCache(m_pRedis, userId, dbUser, 0);
        }
        SendGetUserInfoResponse(pMsgHeader, NGX_USER_OK, dbUser.id, dbUser.username);
        return true;
    }

    if (m_pRedis != nullptr) {
        CUserCacheService::SetNullUserCache(m_pRedis, userId, 0);
    }
    SendGetUserInfoResponse(pMsgHeader, NGX_USER_NOT_FOUND, userId, nullptr);
    return false;
}

bool CLogicSocket::_HandlePing(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength)
{
    if(iBodyLength != 0)  //有包体则认为是 非法包
		return false; 

    CLock lock(&pConn->logicPorcMutex); //凡是和本用户有关的访问都考虑用互斥，以免该用户同时发送过来两个命令达到各种作弊目的
    pConn->lastPingTime = time(NULL);   //更新该变量

    //服务器也发送 一个只有包头的数据包给客户端，作为返回的数据
    SendNoBodyPkgToClient(pMsgHeader,_CMD_PING);

    //ngx_log_stderr(0,"成功收到了心跳包并返回结果！");
    return true;
}
