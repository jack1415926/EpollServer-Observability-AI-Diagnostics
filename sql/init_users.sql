-- Epoll Server 用户表初始化脚本
-- 用法（root）：mysql -u root -p < init_users.sql

CREATE DATABASE IF NOT EXISTS epoll_db DEFAULT CHARSET utf8mb4;
USE epoll_db;

CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(56) NOT NULL UNIQUE,
  password VARCHAR(64) NOT NULL COMMENT '演示用明文；生产请改为哈希',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 与 nginx.conf [Mysql] 中账号一致（可按需修改密码）
CREATE USER IF NOT EXISTS 'epoll_user'@'localhost' IDENTIFIED BY 'epoll_pass';
CREATE USER IF NOT EXISTS 'epoll_user'@'127.0.0.1' IDENTIFIED BY 'epoll_pass';
GRANT SELECT, INSERT, UPDATE, DELETE ON epoll_db.* TO 'epoll_user'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON epoll_db.* TO 'epoll_user'@'127.0.0.1';
FLUSH PRIVILEGES;
