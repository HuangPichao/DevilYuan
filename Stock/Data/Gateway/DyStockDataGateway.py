﻿from time import sleep
import pandas as pd
import tushare as ts
import numpy as np

# copy from tushare
try:
    from urllib.request import urlopen, Request
except ImportError:
    from urllib3 import urlopen, Request
    pass

from pandas.compat import StringIO
from tushare.stock import cons as ct

from DyCommon.DyCommon import *
from EventEngine.DyEvent import *
from ..DyStockDataCommon import *
from .DyStockDataWind import *
from ...Common.DyStockCommon import *
from .DyStockDataTicksGateway import DyStockDataTicksGateway
from .DyStockDataTdx import DyStockDataTdx


class DyStockDataGateway(object):
    """
        股票数据网络接口
        日线数据从Wind获取，分笔数据可以从新浪，腾讯，网易，通达信获取
    """
    tradeDaysMode = "Verify" # default is verify between Wind and TuShare

    tuShareDaysSleepTimeConst = 0 # It's set by config
    tuShareDaysSleepTime = 0
    tuShareDaysSleepTimeStep = 5


    def __init__(self, eventEngine, info, registerEvent=True):
        self._eventEngine = eventEngine
        self._info = info

        if DyStockCommon.WindPyInstalled:
            self._wind = DyStockDataWind(self._info)
        else:
            self._wind = None

        if registerEvent:
            self._registerEvent()

    def _registerEvent(self):
        """
            register events for each ticks gateway for each hand
        """
        # new DyStockDataTicksGateway instance for each ticks hand to avoid mutex
        self._ticksGateways = [DyStockDataTicksGateway(self._eventEngine, self._info, i) for i in range(DyStockDataEventHandType.stockHistTicksHandNbr)]

    def _getTradeDaysFromTuShare(self, startDate, endDate):
        try:
            df = ts.trade_cal()

            df = df.set_index('calendarDate')
            df = df[startDate:endDate]
            dfDict = df.to_dict()

            # get trade days
            dates = DyTime.getDates(startDate, endDate, strFormat=True)
            tDays = []
            for date in dates:
                if dfDict['isOpen'][date] == 1:
                    tDays.append(date)

            return tDays

        except Exception as ex:
            self._info.print("从TuShare获取[{}, {}]交易日数据异常: {}".format(startDate, endDate, str(ex)), DyLogData.error)
            
        return None

    def _determineTradeDays(self, windTradeDays, tuShareTradeDays):
        def _errorResult():
            if self.tradeDaysMode == "Verify":
                return None
            elif self.tradeDaysMode == "Wind":
                return windTradeDays
            else:
                return tuShareTradeDays

        logType = DyLogData.error if self.tradeDaysMode == "Verify" else DyLogData.warning

        if windTradeDays is None or tuShareTradeDays is None or len(windTradeDays) != len(tuShareTradeDays):
            self._info.print("Wind交易日数据{}跟TuShare{}不一致".format(windTradeDays, tuShareTradeDays), logType)
            return _errorResult()
            
        for x, y in zip(windTradeDays, tuShareTradeDays):
            if x != y:
                self._info.print("Wind交易日数据{}跟TuShare{}不一致".format(windTradeDays, tuShareTradeDays), logType)
                return _errorResult()

        return windTradeDays # same

    def getTradeDays(self, startDate, endDate):
        """
            Wind可能出现数据错误，所以需要从其他数据源做验证
        """
        # from Wind
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource:
            windTradeDays = self._wind.getTradeDays(startDate, endDate)
            tradeDays = windTradeDays

        # always get from TuShare
        tuShareTradeDays = self._getTradeDaysFromTuShare(startDate, endDate)
        tradeDays = tuShareTradeDays

        # verify
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource:
            tradeDays = self._determineTradeDays(windTradeDays, tuShareTradeDays)

        return tradeDays

    def getStockCodes(self):
        """
            获取股票代码表
        """
        # from Wind
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource:
            if self._wind is None:
                self._info.print("没有安装WindPy", DyLogData.error)
                return None

            windCodes = self._wind.getStockCodes()
            codes = windCodes

        # from TuShare
        if 'TuShare' in DyStockCommon.defaultHistDaysDataSource:
            tuShareCodes = self._getStockCodesFromTdx()
            codes = tuShareCodes

        # verify
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource and 'TuShare' in DyStockCommon.defaultHistDaysDataSource:
            if windCodes is None or tuShareCodes is None or len(windCodes) != len(tuShareCodes):
                self._info.print("Wind股票代码表跟TuShare不一致", DyLogData.error)
                return None

            for code, name in windCodes.items():
                name_ = tuShareCodes.get(code)
                if name_ is None or name_ != name:
                    self._info.print("Wind股票代码表跟TuShare不一致", DyLogData.error)
                    return None

        return codes

    def getSectorStockCodes(self, sectorCode, startDate, endDate):
        return self._wind.getSectorStockCodes(sectorCode, startDate, endDate)

    def getDays(self, code, startDate, endDate, fields, name=None):
        """
            获取股票日线数据
            @return: MongoDB BSON format like [{'datetime': value, 'indicator': value}]
                     None - erros
        """
        # get from Wind
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource:
            windDf = self._wind.getDays(code, startDate, endDate, fields, name)
            df = windDf

        # get from TuShare
        if 'TuShare' in DyStockCommon.defaultHistDaysDataSource:
            tuShareDf = self._getDaysFromTuShare(code, startDate, endDate, fields, name)
            df = tuShareDf

        # verify data
        if 'Wind' in DyStockCommon.defaultHistDaysDataSource and 'TuShare' in DyStockCommon.defaultHistDaysDataSource:
            if windDf is None or tuShareDf is None or windDf.shape[0] != tuShareDf.shape[0]:
                self._info.print("{}({})日线数据[{}, {}]: Wind和TuShare不相同".format(code, name, startDate, endDate), DyLogData.error)
                return None

            # remove adjfactor because Sina adjfactor is different with Wind
            fields_ = [x for x in fields if x != 'adjfactor']
            fields_ = ['datetime'] + fields_

            if (windDf[fields_].values != tuShareDf[fields_].values).sum() > 0:
                self._info.print("{}({})日线数据[{}, {}]: Wind和TuShare不相同".format(code, name, startDate, endDate), DyLogData.error)
                return None

        # BSON
        return None if df is None else list(df.T.to_dict().values())

    def isNowAfterTradingTime(self):
        today = datetime.now().strftime("%Y-%m-%d")

        for _ in range(3):
            days = self.getTradeDays(today, today)
            if days is not None:
                break

            sleep(1)
        else:
            self._info.print("@DyStockDataGateway.isNowAfterTradingTime: 获取交易日数据[{}, {}]失败3次".format(today, today), DyLogData.error)
            return None # error

        if today in days:
            year, month, day = today.split('-')
            afterTradeTime = datetime(int(year), int(month), int(day), 18, 0, 0)

            if datetime.now() < afterTradeTime:
                return False

        return True

    def _getDaysFromTuShareOld(self, code, startDate, endDate, fields, name=None, verify=False):
        """
            从tushare获取股票日线数据。
            保持跟Wind接口一致，由于没法从网上获取净流入量和金额，所以这两个字段没有。
            策略角度看，其实这两个字段也没什么用。
            @verify: True - 不同网上的相同字段会相互做验证。
            @return: df['datetime', indicators]
                     None - errors
                     [] - no data
        """
        code = code[:-3]

        try:
            # 从凤凰网获取换手率，成交量是手（没有整数化过，比如2004.67手）
            ifengDf = ts.get_hist_data(code, startDate, endDate).sort_index()

            # 以无复权方式从腾讯获取OHCLV，成交量是手（整数化过）
            if verify:
                tcentDf = ts.get_k_data(code, startDate, endDate, autype=None).sort_index()

            # 从新浪获取复权因子，成交量是股。新浪的数据是后复权的，无复权方式是tushare根据复权因子实现的。
            sinaDf = ts.get_h_data(code, startDate, endDate, autype=None, drop_factor=False)
            if sinaDf is None: # If no data, TuShare return None
                sinaDf = pd.DataFrame(columns=['open', 'high', 'close', 'low', 'volume', 'amount', 'factor'])
            else:
                sinaDf = sinaDf.sort_index()
        except Exception as ex:
            self._info.print("从TuShare获取{}({})日线数据[{}, {}]失败: {}".format(code, name, startDate, endDate, ex), DyLogData.error)
            return None

        # 数据相互验证
        if verify:
            # OHLC
            for indicator in ['open', 'high', 'close', 'low']:
                if len(tcentDf[indicator].values) != len(sinaDf[indicator].values):
                    self._info.print("{}({})日线数据OHLC[{}, {}]: 腾讯和新浪不相同".format(code, name, startDate, endDate), DyLogData.error)
                    return None

                if (tcentDf[indicator].values != sinaDf[indicator].values).sum() > 0:
                    self._info.print("{}({})日线数据OHLC[{}, {}]: 腾讯和新浪不相同".format(code, name, startDate, endDate), DyLogData.error)
                    return None

            # volume
            if len(ifengDf['volume'].values) != len(sinaDf['volume'].values):
                self._info.print("{}({})日线数据Volume[{}, {}]: 凤凰网和新浪不相同".format(code, name, startDate, endDate), DyLogData.error)
                return None

            if (np.round(ifengDf['volume'].values*100) != np.round(sinaDf['volume'].values)).sum() > 0:
                self._info.print("{}({})日线数据Volume[{}, {}]: 凤凰网和新浪不相同".format(code, name, startDate, endDate), DyLogData.error)
                return None

        # construct new DF
        df = pd.concat([sinaDf[['open', 'high', 'close', 'low', 'volume', 'amount', 'factor']], ifengDf['turnover']], axis=1)
        df.index.name = None

        # change to Wind's indicators
        df.reset_index(inplace=True) # 把时间索引转成列
        df.rename(columns={'index': 'datetime', 'amount': 'amt', 'turnover': 'turn', 'factor': 'adjfactor'}, inplace=True)

        # 把日期的HH:MM:SS转成 00:00:00
        df['datetime'] = df['datetime'].map(lambda x: x.strftime('%Y-%m-%d'))
        df['datetime'] = pd.to_datetime(df['datetime'], format='%Y-%m-%d')

        # select according @fields
        df = df[['datetime'] + fields]

        return df

    def _getStockCodesFromTuShare(self):
        self._info.print("开始从TuShare获取股票代码表...")

        try:
            df = ts.get_today_all() # it's slow because TuShare will get one page by one page
        except Exception as ex:
            self._info.print("从TuShare获取股票代码表异常: {}".format(ex), DyLogData.error)
            return None

        if df is None or df.empty:
            self._info.print("从TuShare获取股票代码表为空", DyLogData.error)
            return None

        codes = {}
        data = df[['code', 'name']].values.tolist()
        for code, name in data:
            if code[0] == '6':
                codes[code + '.SH'] = name
            else:
                codes[code + '.SZ'] = name

        self._info.print("从TuShare获取股票代码表成功")
        return codes

    def _getStockCodesFromTdx(self):
        self._info.print("开始从TDX获取股票代码表...")

        tdx = DyStockDataTdx(-1, self._info)
        df = tdx.getStockCodes()
        tdx.close()
        if df is None:
            self._info.print("从TDX获取股票代码表失败", DyLogData.error)
            return None

        codes = {}
        data = df[['code', 'name']].values.tolist()
        for code, name in data:
            if code[0] == '6':
                codes[code + '.SH'] = name
            else:
                codes[code + '.SZ'] = name

        self._info.print("从TDX获取股票代码表成功")
        return codes

    def _getDaysFrom163(self, code, startDate, endDate, retry_count=3, pause=0.001):
        """
            从网易获取个股日线数据，指数和基金（ETF）除外
            @code: DevilYuan Code

        """
        symbol = ('0' + code[:6]) if code[-2:] == 'SH' else ('1' + code[:6])

        for _ in range(retry_count):
            sleep(pause)
            try:
                url = 'http://quotes.money.163.com/service/chddata.html?code={}&start={}&end={}&fields=TCLOSE;HIGH;LOW;TOPEN;TURNOVER;VOTURNOVER;VATURNOVER'
                url = url.format(symbol, startDate.replace('-', ''), endDate.replace('-', ''))
                re = Request(url)
                lines = urlopen(re, timeout=10).read()
                lines = lines.decode('GBK') 
                df = pd.read_table(StringIO(lines),
                                   sep=',',
                                   names=['date', 'code', 'name', 'close', 'high', 'low', 'open', 'turnover', 'volume', 'amount'],
                                   skiprows=[0])
            except Exception as e:
                print(e)
                ex = e
            else:
                df = df[['date', 'open', 'high', 'close', 'low', 'volume', 'amount', 'turnover']] # return columns
                df = df.set_index('date')
                df = df.sort_index(ascending=False)
                return df
        raise ex

    def _getCodeDaysFromTuShare(self, code, startDate, endDate, fields, name=None):
        """
            从TuShare获取个股日线数据
        """
        print("{}, {} ~ {}".format(code, startDate, endDate))

        tuShareCode = code[:-3]

        try:
            # 从网易获取换手率
            netEasyDf = self._getDaysFrom163(code, startDate, endDate).sort_index()
            netEasyDf = netEasyDf[netEasyDf['volume'] > 0] # drop停牌日期的数据

            netEasyDf.index = pd.to_datetime(netEasyDf.index, format='%Y-%m-%d')

            # 从新浪获取复权因子，成交量是股。新浪的数据是后复权的，无复权方式是tushare根据复权因子实现的。
            sleepTime = self.tuShareDaysSleepTimeConst + self.tuShareDaysSleepTime
            try:
                sinaDf = ts.get_h_data(tuShareCode, startDate, endDate, autype=None, drop_factor=False, pause=sleepTime)
            except IOError: # We think Sina is anti-crawling
                self.tuShareDaysSleepTime += self.tuShareDaysSleepTimeStep
                print("Sina is anti-crawling, setting additional sleep time to {}s for each request".format(self.tuShareDaysSleepTime))
                raise

            if self.tuShareDaysSleepTime > 0:
                self.tuShareDaysSleepTime -= self.tuShareDaysSleepTimeStep

            if sinaDf is None or sinaDf.empty: # If no data, TuShare return None
                sinaDf = pd.DataFrame(columns=['open', 'high', 'close', 'low', 'volume', 'amount', 'factor'])
            else:
                sinaDf = sinaDf.sort_index()
        except Exception as ex:
            self._info.print("从TuShare获取{}({})日线数据[{}, {}]失败: {}".format(code, name, startDate, endDate, ex), DyLogData.warning)
            return None

        # construct new DF
        try:
            df = pd.concat([sinaDf[['open', 'high', 'close', 'low', 'volume', 'amount', 'factor']], netEasyDf['turnover']], axis=1)
            df.index.name = None
        except Exception as ex:
            print("netEasyDf")
            print(netEasyDf)
            print("sinaDf")
            print(sinaDf)

            self._info.print("从TuShare获取的{}({})日线数据[{}, {}]格式错误: {}".format(code, name, startDate, endDate, ex), DyLogData.warning)
            return None

        if df.isnull().sum().sum() > 0:
            self._info.print("{}({})新浪日线和网易日线数据不一致[{}, {}]".format(code, name, startDate, endDate), DyLogData.warning)
            return None

        # change to Wind's indicators
        df.reset_index(inplace=True) # 把时间索引转成列
        df.rename(columns={'index': 'datetime', 'amount': 'amt', 'turnover': 'turn', 'factor': 'adjfactor'}, inplace=True)

        # 把日期的HH:MM:SS转成 00:00:00
        df['datetime'] = df['datetime'].map(lambda x: x.strftime('%Y-%m-%d'))
        df['datetime'] = pd.to_datetime(df['datetime'], format='%Y-%m-%d')

        # select according @fields
        df = df[['datetime'] + fields]

        return df

    def _getIndexDaysFromTuShare(self, code, startDate, endDate, fields, name=None):
        """
            从TuShare获取指数日线数据
        """
        tuShareCode = code[:-3]

        sleepTime = self.tuShareDaysSleepTimeConst + self.tuShareDaysSleepTime
        try:
            df = ts.get_h_data(tuShareCode, startDate, endDate, index=True, pause=sleepTime)
            if df is None or df.empty: # If no data, TuShare return None
                df = pd.DataFrame(columns=['open', 'high', 'close', 'low', 'volume', 'amount'])
            else:
                df = df.sort_index()
        except Exception as ex:
            self._info.print("从TuShare获取{}({})日线数据[{}, {}]失败: {}".format(code, name, startDate, endDate, ex), DyLogData.error)
            return None

        # no turn and factor for index
        df['turnover'] = 0
        df['factor'] = 1
        df.index.name = None

        # change to Wind's indicators
        df.reset_index(inplace=True) # 把时间索引转成列
        df.rename(columns={'index': 'datetime', 'amount': 'amt', 'turnover': 'turn', 'factor': 'adjfactor'}, inplace=True)

        # 把日期的HH:MM:SS转成 00:00:00
        df['datetime'] = df['datetime'].map(lambda x: x.strftime('%Y-%m-%d'))
        df['datetime'] = pd.to_datetime(df['datetime'], format='%Y-%m-%d')

        # select according @fields
        df = df[['datetime'] + fields]

        return df

    def _getFundDaysFromTuShare(self, code, startDate, endDate, fields, name=None):
        """
            从tushare获取基金（ETF）日线数据。
            # !!!TuShare没有提供换手率，复权因子和成交额，所以只能假设。
            # 策略针对ETF的，需要注意。
        """
        tuShareCode = code[:-3]

        sleepTime = self.tuShareDaysSleepTimeConst + self.tuShareDaysSleepTime
        try:
            # 以无复权方式从腾讯获取OHCLV，成交量是手（整数化过）
            # 此接口支持ETF日线数据
            df = ts.get_k_data(tuShareCode, startDate, endDate, autype=None, pause=sleepTime).sort_index()
        except Exception as ex:
            self._info.print("从TuShare获取{}({})日线数据[{}, {}]失败: {}".format(code, name, startDate, endDate, ex), DyLogData.error)
            return None

        df['volume'] = df['volume']*100

        # !!!TuShare没有提供换手率，复权因子和成交额，所以只能假设。
        # 策略针对ETF的，需要注意。
        df['turnover'] = 0
        df['factor'] = 1
        df['amount'] = 0
        df.index.name = None

        # change to Wind's indicators
        df.rename(columns={'date': 'datetime', 'amount': 'amt', 'turnover': 'turn', 'factor': 'adjfactor'}, inplace=True)

        # 把日期的HH:MM:SS转成 00:00:00
        df['datetime'] = pd.to_datetime(df['datetime'], format='%Y-%m-%d')

        # select according @fields
        df = df[['datetime'] + fields]

        return df

    def _getDaysFromTuShare(self, code, startDate, endDate, fields, name=None):
        """
            从tushare获取股票日线数据（含指数和基金（ETF））。
            !!!TuShare没有提供换手率和复权因子，所以只能假设。
            策略针对ETF的，需要注意。
            保持跟Wind接口一致，由于没法从网上获取净流入量和金额，所以这两个字段没有。
            策略角度看，其实这两个字段也没什么用。
            @verify: True - 不同网上的相同字段会相互做验证。
            @return: df['datetime', indicators]
                     None - errors
                     [] - no data
        """
        if code in DyStockCommon.indexes:
            return self._getIndexDaysFromTuShare(code, startDate, endDate, fields, name)
        
        if code in DyStockCommon.funds:
            return self._getFundDaysFromTuShare(code, startDate, endDate, fields, name)

        return self._getCodeDaysFromTuShare(code, startDate, endDate, fields, name)