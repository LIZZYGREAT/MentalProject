# 飞书用户访问令牌获取工具

## 简介

这是一个用于获取飞书用户访问令牌的命令行工具。通过该工具，您可以方便地获取和刷新飞书API的用户访问令牌，用于后续的API调用。

## 功能特点

- 自动打开浏览器访问授权URL，简化授权流程
- 生成飞书授权URL，引导用户完成授权流程
- 使用授权码获取用户访问令牌
- 刷新现有的用户访问令牌
- 保存和加载令牌信息到文件
- 自动检查令牌是否过期
- 支持从.env文件加载环境变量配置

## 环境配置

在使用本工具之前，您需要设置以下环境变量（支持两种命名方式）：

### 方式1：直接命名
- `APP_ID`: 飞书应用的App ID（必须）
- `APP_SECRET`: 飞书应用的App Secret（必须）

### 方式2：FEISHU前缀命名
- `FEISHU_APP_ID`: 飞书应用的App ID（必须）
- `FEISHU_APP_SECRET`: 飞书应用的App Secret（必须）

### 注意事项
- 回调地址已固定为：`https://open.feishu.cn/api-explorer/loading`，这是飞书API Explorer页面的标准回调地址
- 本工具支持从`.env`文件加载环境变量，您可以在项目根目录创建`.env`文件并写入上述配置

### 从.env文件加载配置

创建一个名为`.env`的文件，内容如下：

```
APP_ID=您的App ID
APP_SECRET=您的App Secret
# 或者使用FEISHU前缀
# FEISHU_APP_ID=您的App ID
# FEISHU_APP_SECRET=您的App Secret
```

### Windows系统设置环境变量

在命令提示符中运行：

```batch
setx APP_ID "您的App ID"
setx APP_SECRET "您的App Secret"
```

设置完环境变量后，需要重新打开命令提示符窗口。

### Linux/Mac系统设置环境变量

在终端中运行：

```bash
export APP_ID="您的App ID"
export APP_SECRET="您的App Secret"
```

或者将这些命令添加到`~/.bashrc`或`~/.zshrc`文件中，使其永久生效。

## 安装依赖

本工具依赖于以下Python包：

```bash
pip install -r requirements.txt
```

requirements.txt包含以下依赖：
- requests==2.31.0
- python-dotenv==1.0.0

## 基本使用

### 步骤1：确保环境变量已正确设置

请确保您已经按照上述说明设置了所有必要的环境变量。

### 步骤2：运行程序

在命令行中执行：

```bash
python get_information.py
```

### 步骤3：选择操作

程序将显示一个菜单，您可以选择：

1. 获取新的用户访问令牌
2. 刷新现有令牌
0. 退出

## 获取新的用户访问令牌

1. 选择菜单中的选项1
2. 程序会提示您是否需要获取刷新令牌（默认是）
3. 程序将显示从环境变量加载的回调地址
4. 系统会**自动打开浏览器**访问授权URL（无需手动复制URL）
5. 登录您的飞书账号并同意授权
6. 授权成功后，浏览器会跳转到飞书API Explorer页面（https://open.feishu.cn/api-explorer/loading）
7. 在浏览器地址栏中找到`code`参数并复制其值
8. 将复制的code参数值粘贴到程序中
9. 程序将显示获取到的令牌信息，并询问是否保存到文件

### 关于自动获取授权码的说明

由于使用了飞书API Explorer的标准回调地址（https://open.feishu.cn/api-explorer/loading），系统无法自动捕获授权回调中的code参数。这是因为：

1. 该回调地址属于飞书官方域名，我们的本地程序无法直接访问或监听该域名下的请求
2. 出于安全考虑，浏览器会限制跨域访问和第三方脚本操作

因此，在使用飞书API Explorer回调地址的情况下，您需要手动从浏览器地址栏复制code参数的值。这种方式虽然需要一步手动操作，但保证了与飞书API Explorer的完全兼容性。

## 刷新现有令牌

1. 选择菜单中的选项2
2. 程序会询问是否从文件加载刷新令牌（默认是）
3. 如果选择从文件加载，请指定令牌文件路径（默认是user_token.json）
4. 如果没有从文件加载到刷新令牌，则需要手动输入刷新令牌
5. 程序将显示刷新后的令牌信息，并询问是否保存到文件

## 常见问题

### 授权回调地址不匹配

如果遇到"redirect_uri不匹配"的错误，请确保：
1. 环境变量中设置的`REDIRECT_URI`与飞书开发者后台配置的回调地址完全一致
2. URL编码格式也必须一致

### 如何获取飞书应用的App ID和App Secret？

1. 访问[飞书开放平台](https://open.feishu.cn/)
2. 登录您的账号并创建一个新应用
3. 在应用的"凭证与基础信息"页面可以找到App ID和App Secret
4. 在"安全设置"页面配置回调地址

---

# Feishu User Access Token Tool

## Introduction

This is a command-line tool for obtaining Feishu (Lark) user access tokens. With this tool, you can conveniently obtain and refresh user access tokens for Feishu API, which can be used for subsequent API calls.

## Features

- Generate Feishu authorization URL to guide users through the authorization process
- Obtain user access tokens using authorization codes
- Refresh existing user access tokens
- Save and load token information to files
- Automatically check if tokens have expired

## Environment Configuration

Before using this tool, you need to set the following environment variables:

- `APP_ID`: App ID of your Feishu application (required)
- `APP_SECRET`: App Secret of your Feishu application (required)
- `REDIRECT_URI`: Authorization callback URL, must be consistent with the callback URL configured in the Feishu Developer后台 (optional, default value: `https://open.feishu.cn/api-explorer/loading`)

### Setting Environment Variables on Windows

Run in Command Prompt:

```batch
setx APP_ID "Your App ID"
setx APP_SECRET "Your App Secret"
setx REDIRECT_URI "https://open.feishu.cn/api-explorer/loading"
```

After setting environment variables, you need to reopen the Command Prompt window.

### Setting Environment Variables on Linux/Mac

Run in Terminal:

```bash
export APP_ID="Your App ID"
export APP_SECRET="Your App Secret"
export REDIRECT_URI="https://open.feishu.cn/api-explorer/loading"
```

Or add these commands to your `~/.bashrc` or `~/.zshrc` file to make them permanent.

## Installing Dependencies

This tool depends on the following Python package:

```bash
pip install requests
```

## Basic Usage

### Step 1: Ensure Environment Variables are Correctly Set

Make sure you have set all necessary environment variables as described above.

### Step 2: Run the Program

Execute in command line:

```bash
python get_information.py
```

### Step 3: Select Operation

The program will display a menu where you can choose:

1. Obtain a new user access token
2. Refresh existing token
0. Exit

## Obtaining a New User Access Token

1. Select option 1 from the menu
2. The program will ask if you need to obtain a refresh token (default is yes)
3. The program will display the callback URL loaded from environment variables
4. Copy and open the displayed authorization URL in a browser
5. Log in to your Feishu account and agree to the authorization
6. After successful authorization, the browser will redirect to the callback URL, and the URL will contain the authorization code (code parameter)
7. Copy the value of the code parameter from the URL and paste it into the program
8. The program will display the obtained token information and ask if you want to save it to a file

## Refreshing Existing Token

1. Select option 2 from the menu
2. The program will ask if you want to load the refresh token from a file (default is yes)
3. If you choose to load from a file, please specify the token file path (default is user_token.json)
4. If no refresh token is loaded from the file, you need to manually enter the refresh token
5. The program will display the refreshed token information and ask if you want to save it to a file

## Common Issues

### Authorization Callback URL Mismatch

If you encounter a "redirect_uri mismatch" error, please ensure:
1. The `REDIRECT_URI` set in the environment variable is exactly the same as the callback URL configured in the Feishu Developer后台
2. The URL encoding format must also be consistent

### How to Obtain App ID and App Secret for Feishu Application?

1. Visit [Feishu Open Platform](https://open.feishu.cn/)
2. Log in to your account and create a new application
3. You can find App ID and App Secret on the "Credentials and Basic Information" page of the application
4. Configure the callback URL on the "Security Settings" page