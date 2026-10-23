
#ifndef __NGX_LOGICCOMM_H__
#define __NGX_LOGICCOMM_H__

#include <stdint.h>

//收发命令宏定义

#define _CMD_START	                    0  
#define _CMD_PING				   	    _CMD_START + 0   //ping命令【心跳包】
#define _CMD_REGISTER 		            _CMD_START + 5   //注册
#define _CMD_LOGIN 		                _CMD_START + 6   //登录
#define _CMD_GET_USER_INFO              _CMD_START + 7   //按 userId 查询用户信息

// GetUserInfo 业务错误码（回包 iResult）
#define NGX_USER_OK           0
#define NGX_USER_NOT_FOUND    1
#define NGX_USER_ERR_BADREQ   2
#define NGX_USER_ERR_BACKEND  3

//结构定义------------------------------------
#pragma pack (1) //对齐方式,1字节对齐【结构之间成员不做任何字节对齐：紧密的排列在一起】

typedef struct _STRUCT_REGISTER
{
	int           iType;          //类型
	char          username[56];   //用户名 
	char          password[40];   //密码

}STRUCT_REGISTER, *LPSTRUCT_REGISTER;

typedef struct _STRUCT_LOGIN
{
	int           iResult;        //响应：0成功 1失败 2DB错误 3池超时；请求时客户端填0即可
	char          username[56];   //用户名 
	char          password[40];   //密码

}STRUCT_LOGIN, *LPSTRUCT_LOGIN;

typedef struct _STRUCT_GET_USER_INFO_REQ
{
	int64_t       userId;         //网络字节序（大端）
}STRUCT_GET_USER_INFO_REQ, *LPSTRUCT_GET_USER_INFO_REQ;

typedef struct _STRUCT_GET_USER_INFO_RESP
{
	int           iResult;        //见 NGX_USER_*
	int64_t       userId;
	char          username[56];
}STRUCT_GET_USER_INFO_RESP, *LPSTRUCT_GET_USER_INFO_RESP;

#pragma pack() //取消指定对齐，恢复缺省对齐

#endif
