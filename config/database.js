/**
 * 数据库连接配置
 * 支持 MySQL / MariaDB
 */

const mysql = require('mysql2/promise');

// 创建连接池
const pool = mysql.createPool({
  host: process.env.DB_HOST || 'localhost',
  user: process.env.DB_USER || 'root',
  password: process.env.DB_PASSWORD || '',
  database: process.env.DB_NAME || 'auth_system',
  waitForConnections: true,
  connectionLimit: 10,
  queueLimit: 0,
  enableKeepAlive: true,
  keepAliveInitialDelay: 0
});

// 测试连接
async function testConnection() {
  try {
    const connection = await pool.getConnection();
    console.log('✓ 数据库连接成功');
    connection.release();
    return true;
  } catch (error) {
    console.error('✗ 数据库连接失败:', error.message);
    return false;
  }
}

// 暴露 execute 方法（支持参数化查询）
const db = {
  execute: async (sql, params) => {
    return await pool.execute(sql, params);
  },
  query: async (sql, params) => {
    return await pool.query(sql, params);
  },
  getConnection: async () => {
    return await pool.getConnection();
  }
};

module.exports = db;
module.exports.testConnection = testConnection;
