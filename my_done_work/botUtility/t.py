import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from loginProxy import get_libic_session

username = input("请输入学号：")
password = input("请输入密码：")

session, response = get_libic_session(username, password)

print("返回状态码：", response.status_code)
print("返回内容：")
print(response.text)
