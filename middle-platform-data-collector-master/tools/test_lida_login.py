import requests, json

# 登录
r = requests.post('https://api-lida-course.qimingdaren.com/auth/account/login',
    json={'username': '13931822731', 'password': 'tjQoH66O'})
token = r.json()['data']['token']
headers = {'token': token}

# 获取用户完整信息（包含角色、权限）
r2 = requests.get('https://api-lida-course.qimingdaren.com/auth/user/info', headers=headers)
user = r2.json()
print('=== 用户信息 ===')
print(json.dumps(user, ensure_ascii=False, indent=2)[:3000])

# 尝试查询学校相关的统计
print('\n=== 学校信息 ===')
for school in user.get('data', {}).get('schoolIds', []):
    sid = school.get('schoolId')
    sname = school.get('schoolName')
    print(f'  School {sid}: {sname}')

# 尝试各种统计 API
print('\n=== 搜索数据 API ===')
paths_to_try = [
    # 集体备课统计
    '/collectivePreparation/usageOverview',
    '/collectivePreparation/stats',
    '/collectivePreparation/overview',
    '/collective/preparation/usage/overview',
    '/lesson/preparation/stats',
    # 平台使用数据
    '/platform/usage/overview',
    '/platform/stats',
    '/data/platform/usage',
    '/statistics/platform',
    # 通用统计
    '/data/statistics',
    '/statistics/overview',
    '/report/usage',
    '/dashboard',
    # 可能的前缀
    '/api/data/usage',
    '/api/stats/overview',
]

for path in paths_to_try:
    r = requests.get(f'https://api-lida-course.qimingdaren.com{path}', headers=headers)
    status = r.status_code
    if status not in (404, 405):
        try:
            body = json.dumps(r.json(), ensure_ascii=False)[:300]
        except:
            body = r.text[:200]
        print(f'  GET {path}: {status} => {body}')

# 尝试 POST 方式
print('\n=== 尝试 POST ===')
for path in ['/collectivePreparation/usageOverview', '/platform/usage/overview', '/statistics/usage']:
    r = requests.post(f'https://api-lida-course.qimingdaren.com{path}', 
        headers={**headers, 'Content-Type': 'application/json'},
        json={"schoolId": 3})
    if r.status_code not in (404, 405):
        print(f'  POST {path}: {r.status_code} => {json.dumps(r.json(), ensure_ascii=False)[:300]}')

# 查看所有菜单的正确路由（不限于 data）
print('\n=== 完整路由列表 ===')
r3 = requests.get('https://api-lida-course.qimingdaren.com/sys/role/front-route', headers=headers)
routes = r3.json()
# 递归查找所有 component 和 path
def extract_routes(obj, prefix=''):
    results = []
    if isinstance(obj, dict):
        comp = obj.get('component', '')
        path = obj.get('path', '')
        name = obj.get('name', '')
        title = obj.get('title', '') or obj.get('meta', {}).get('title', '')
        if comp:
            results.append(f'{prefix}component={comp}, path={path}, name={name}, title={title}')
        for k, v in obj.items():
            if k == 'children' and isinstance(v, list):
                for child in v:
                    results.extend(extract_routes(child, prefix + '  '))
            elif isinstance(v, (dict, list)):
                results.extend(extract_routes(v, prefix))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(extract_routes(item, prefix))
    return results

all_routes = extract_routes(routes)
for rt in all_routes:
    print(f'  {rt[:150]}')
