# 飞书日历工具使用指南

## 工具简介

这个工具是一个整合了飞书API认证和日历查询功能的实用程序，可以帮助你轻松获取飞书日历中的日程事件。它支持两种认证方式：应用级认证和用户级认证，并且能够将查询结果保存为JSON文件方便后续使用。

## 功能特点

1. **双重认证方式**：支持使用应用ID和密钥，或使用用户访问令牌进行认证
2. **灵活的时间范围**：可以自定义查询的开始和结束时间
3. **数据过滤与验证**：自动过滤不在指定日期范围内的事件
4. **详细的日志输出**：显示每个步骤的执行情况和结果统计
5. **结果持久化**：将查询到的事件保存为JSON文件

## 如何使用

### 基本使用方法

打开命令行，进入代码所在的文件夹，然后运行以下命令：

```
python calendar_tool_combined.py
```

程序会自动使用默认的应用ID和密钥进行认证，并获取今天8:00到23:00的日程事件。

### 修改参数

如果你需要修改认证信息或查询参数，可以打开`calendar_tool_combined.py`文件，修改以下几个地方：

#### 1. 修改应用ID和密钥

在`example_with_app_auth()`函数中，找到以下代码并替换为你的信息：

```python
calendar_tool = FeishuCalendarTool(
    app_id="cli_a74daa9319ff500c",  # 替换为实际的应用ID
    app_secret="3IwKblhV29gurCoAj37oQcInvczvgEx7"  # 替换为实际的应用密钥
)
```

#### 2. 自定义查询时间范围

如果你想更改查询的时间范围，可以修改调用`get_today_events()`方法时的参数：

```python
# 查询今天9:00到18:00的事件
events = calendar_tool.get_today_events(start_hour=9, end_hour=18)
```

#### 3. 使用用户访问令牌

如果你想使用用户访问令牌进行认证，可以取消`main()`函数中`example_with_user_token()`的注释，并修改`example_with_user_token()`函数中的令牌：

```python
# 在main()函数中取消下面这行的注释
# example_with_user_token()

# 并修改example_with_user_token()函数中的令牌
calendar_tool = FeishuCalendarTool(
    user_access_token="你的用户访问令牌"  # 替换为实际的用户访问令牌
)
```

### 查看结果

查询完成后，程序会显示查询结果的统计信息，并且将结果保存到`calendar_data`文件夹中。文件名格式为`calendar_年月日.json`，例如`calendar_20231001.json`。

## 文件说明

- **calendar_tool_combined.py**: 主要的程序文件，包含了所有功能实现
- **calendar_data/**: 保存日历数据的文件夹

## 代码结构解析

程序的核心是`FeishuCalendarTool`类，它包含了以下主要方法：

1. **create_client()**: 创建飞书API客户端
2. **calculate_today_time_range()**: 计算今天的时间戳范围
3. **extract_event_data()**: 从事件对象中提取有用信息
4. **fetch_calendar_events()**: 从飞书API获取事件列表
5. **save_events_to_json()**: 将事件保存为JSON文件
6. **get_today_events()**: 整合所有步骤，获取今天的事件

## 常见问题

1. **认证失败怎么办？**
   - 检查应用ID和密钥是否正确
   - 确保应用已经获得了必要的权限
   - 如果使用用户访问令牌，确保令牌没有过期

2. **为什么没有查询到事件？**
   - 检查日历ID是否正确
   - 确认查询的日期和时间范围内确实有事件
   - 查看日志输出，了解是否有过滤掉的事件

3. **如何获取用户访问令牌？**
   - 可以使用`get_token.py`文件中的功能获取授权码，然后使用授权码换取用户访问令牌

## 学习建议

如果你想进一步了解代码的工作原理，可以逐步查看每个函数的实现，特别是：

1. 如何计算时间戳
2. 如何调用飞书API
3. 如何处理和过滤API返回的数据

通过修改参数和尝试不同的功能，可以更好地理解这个工具的工作原理。

---

# Feishu Calendar Tool User Guide

## Tool Introduction

This tool is a utility that integrates Feishu API authentication and calendar query functions, helping you easily retrieve schedule events from Feishu calendar. It supports two authentication methods: application-level authentication and user-level authentication, and can save query results as JSON files for later use.

## Features

1. **Dual Authentication Methods**: Supports authentication using app ID and secret, or using user access token
2. **Flexible Time Range**: Can customize the start and end time of the query
3. **Data Filtering and Validation**: Automatically filters events not within the specified date range
4. **Detailed Log Output**: Displays the execution status and result statistics of each step
5. **Result Persistence**: Saves the queried events as JSON files

## How to Use

### Basic Usage

Open the command line, navigate to the folder where the code is located, and run the following command:

```
python calendar_tool_combined.py
```

The program will automatically use the default app ID and secret for authentication and retrieve schedule events from 8:00 to 23:00 today.

### Modifying Parameters

If you need to modify authentication information or query parameters, you can open the `calendar_tool_combined.py` file and modify the following places:

#### 1. Modify App ID and Secret

In the `example_with_app_auth()` function, find the following code and replace it with your information:

```python
calendar_tool = FeishuCalendarTool(
    app_id="cli_a74daa9319ff500c",  # Replace with actual app ID
    app_secret="3IwKblhV29gurCoAj37oQcInvczvgEx7"  # Replace with actual app secret
)
```

#### 2. Customize Query Time Range

If you want to change the query time range, you can modify the parameters when calling the `get_today_events()` method:

```python
# Query events from 9:00 to 18:00 today
events = calendar_tool.get_today_events(start_hour=9, end_hour=18)
```

#### 3. Use User Access Token

If you want to use user access token for authentication, you can uncomment `example_with_user_token()` in the `main()` function and modify the token in the `example_with_user_token()` function:

```python
# Uncomment the following line in the main() function
# example_with_user_token()

# And modify the token in the example_with_user_token() function
calendar_tool = FeishuCalendarTool(
    user_access_token="your_user_access_token"  # Replace with actual user access token
)
```

### Viewing Results

After the query is completed, the program will display statistical information about the query results and save the results to the `calendar_data` folder. The file name format is `calendar_YYYYMMDD.json`, such as `calendar_20231001.json`.

## File Description

- **calendar_tool_combined.py**: The main program file containing all functional implementations
- **calendar_data/**: Folder for saving calendar data

## Code Structure Analysis

The core of the program is the `FeishuCalendarTool` class, which contains the following main methods:

1. **create_client()**: Creates a Feishu API client
2. **calculate_today_time_range()**: Calculates the timestamp range for today
3. **extract_event_data()**: Extracts useful information from event objects
4. **fetch_calendar_events()**: Retrieves event lists from Feishu API
5. **save_events_to_json()**: Saves events as JSON files
6. **get_today_events()**: Integrates all steps to retrieve today's events

## Common Issues

1. **What to do if authentication fails?**
   - Check if the app ID and secret are correct
   - Ensure the app has the necessary permissions
   - If using a user access token, make sure the token has not expired

2. **Why no events are queried?**
   - Check if the calendar ID is correct
   - Confirm that there are indeed events within the queried date and time range
   - Check the log output to understand if there are filtered events

3. **How to obtain a user access token?**
   - You can use the functions in the `get_token.py` file to obtain an authorization code, and then use the authorization code to exchange for a user access token

## Learning Suggestions

If you want to further understand how the code works, you can gradually look at the implementation of each function, especially:

1. How to calculate timestamps
2. How to call Feishu API
3. How to process and filter data returned by the API

By modifying parameters and trying different functions, you can better understand how this tool works.