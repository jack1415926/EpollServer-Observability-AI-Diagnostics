
#ifndef __NGX_C_SLOGIC_H__
#define __NGX_C_SLOGIC_H__

#include <sw/redis++/redis++.h>
#include <stdint.h>
#include <sys/socket.h>
#include "ngx_c_socket.h"

class CLogicSocket : public CSocekt   //继承自父类CScoekt
{
public:
	CLogicSocket();                                                         //构造函数
	virtual ~CLogicSocket();                                                //释放函数
	virtual bool Initialize();                                              //初始化函数

public:

	void Shutdown_subproc() override;
	bool InitializeRedis(); // Worker fork 后、启动线程前创建独立连接池

	//通用收发数据相关函数
	void  SendNoBodyPkgToClient(LPSTRUC_MSG_HEADER pMsgHeader,unsigned short iMsgCode);

	//各种业务逻辑相关函数都在之类
	bool _HandleRegister(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength);
	bool _HandleLogIn(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength);
	bool _HandleGetUserInfo(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength);
	bool _HandlePing(lpngx_connection_t pConn,LPSTRUC_MSG_HEADER pMsgHeader,char *pPkgBody,unsigned short iBodyLength);

	virtual void procPingTimeOutChecking(LPSTRUC_MSG_HEADER tmpmsg,time_t cur_time);      //心跳包检测时间到，该去检测心跳包是否超时的事宜，本函数只是把内存释放，子类应该重新事先该函数以实现具体的判断动作

public:
	virtual void threadRecvProcFunc(char *pMsgBuf);

private:
	void SendGetUserInfoResponse(LPSTRUC_MSG_HEADER header, int result, int64_t userId, const char *username);
	//新增：redis-plus-plus连接池连接池对象指针;
	sw::redis::Redis *m_pRedis = nullptr;
	bool m_redisRateLimitEnabled = true;
	int m_redisRateLimitWindowSec = 60;
	int m_redisRateLimitMaxRequests = 20;
};
#endif
