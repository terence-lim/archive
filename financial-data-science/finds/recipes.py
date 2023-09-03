"""Numerical and data helper functions

- econometrics: unit root, linear regression
- FFT: convolutions and correlations
- data filters
- financial: bonds and risk math

Copyright 2022, Terence Lim

MIT License
"""
import re
import calendar
from datetime import datetime
from typing import Iterable, Mapping, List, Any, NamedTuple, Dict, Tuple
import numpy as np
from numpy.ma import masked_invalid as valid
import pandas as pd
from pandas import DataFrame, Series
from pandas.api import types
from pandas.api.types import is_list_like, is_datetime64_any_dtype, \
    is_integer_dtype, is_string_dtype, is_numeric_dtype
import matplotlib.pyplot as plt
from scipy.stats import chi2, norm, t
from statsmodels.tsa.stattools import adfuller, acf, pacf
from scipy.fft import fft, ifft, rfft, irfft

def check_cuda():
    """Print diagnostics of cuda environment

    Notes:

    - https://pytorch.org/get-started/locally/
    - check cuda version (e.g. 11.4?): Nvidia-smi
    - install matching torch version

      - pip3 install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu116
      - you can specify $ export TORCH\_CUDA\_ARCH\_LIST="8.6" 
        in your environment to force it to build with SM 8.6 support
    """
    import torch
    print('available', torch.cuda.is_available())
    print('version', torch.__version__)
    print('sm86 for rtx3080', torch.cuda.get_arch_list())

######################
#
#  FFT convolutions and correlations
#
######################
class FFT:
    @staticmethod
    def _normalize(X: np.ndarray) -> np.ndarray:
        """Demean columns and divide by norm"""
        X = X - np.mean(X, axis=0)
        X = X / np.linalg.norm(X, axis=0)
        return X

    def correlation(X: np.ndarray, Y: np.ndarray | None) -> Series:
        """Compute cross-correlations of two series, using Convolution Theorem

        Args:
            X, Y: series of observations

        Returns:
            Series of cross-correlation values at every displacement lag

        Notes:

        - Cross-correlation[n] = \sum_m^N f[m] g[n + m]
        - equals Convolution (f * g)[n] = \sum_m^N f[m] g[n - m]

        Examples:

        >>> statsmodels.tsa.stattools.acf(X) 
        >>> fft_correlate(X, X)
        """
        N = len(X)
        if Y is None:
            Y = X
        assert len(Y) == len(X)
    
        # normalize and zero pad to length 2N
        X = np.pad(FFT._normalize(X.reshape(-1, 1)), [(0, N), (0,0)])
        Y = np.flipud(np.pad(FFT._normalize(Y.reshape(-1, 1)), [(0, N), (0,0)]))

        # Convolution Theorem
        conv = irfft(rfft(X, axis=0) * rfft(Y, axis=0), axis=0)
        
        shift = (N // 2) + 1     # only first and last N/2 not due to padding
        window = 2*(N // 2) + 1  # make window length odd to center exactly at 0
        return Series(data=np.roll(conv, shift, axis=0)[:window].reshape(-1),
                      index=np.arange(-(window//2), 1+(window//2)))


    @staticmethod
    def align(X: np.ndarray) -> Tuple:
        """Find max cross-correlation, or best lag, of all pairs of columns
    
        Args:
            X: array with series in columns

        Returns:
            Tuple of list of max cross-correlations between each pair of columns,
            and list of corresponding best lags 

        Notes:

        - Apply convolution theorem to compute cross-correlations at lags
        - For each pair of series, the lag with largest correlation is assumed
          to be the displacement which aligns the presentation of the two series

        Examples:

        >>> FFT.align(np.hstack((X[:-1], X[1:])), corr=False)
        """
        N, M = X.shape
        assert M > 1
        shift = (N // 2) + 1
        window = 2 * (N // 2) + 1
        X = np.pad(FFT._normalize(X), [(0, N), (0,0)])
        Y = rfft(np.flipud(X), axis=0)   # FFT of flipped series
        X = rfft(X, axis=0)              # FFT of original series
        corr = []
        disp = []
        for col in range(M-1):
            # inverse FFT of product of Fourier-transformed series
            conv = irfft(X[:, [col]] * Y[:, col+1:], axis=0) 
            corr.extend(np.max(conv, axis=0))
            disp.extend(((np.argmax(conv, axis=0) + shift) % N) - window)
        return corr, disp


    @staticmethod
    def neweywest(X: np.ndarray) -> List:
        """Compute Newey-West weighted cross-correlation of all pairs of columns

        Args:
            X: array with series in columns

        Returns:
            List of Newey-west weighted cross-correlations

        Notes:

        - First apply convolution theorem to compute all cross-autocorrelations,
        - Then for each pair of series, compute Newey-west weighted correlation
        """
        N, M = X.shape
        assert M > 1
        shift = (N // 2) + 1
        window = 2 * (N // 2) + 1
        L = window // 2

        # Newey-West weights, with peak (1.0) at center of window
        NW = np.array([1 - abs(l)/(L+1) for l in range(-L, L+1, 1)])\
               .reshape(1, -1)

        # Convolution Theorem
        X = np.pad(FFT._normalize(X), [(0, N), (0,0)])
        Y = rfft(np.flipud(X), axis=0)
        X = rfft(X, axis=0)

        # Accumulate results for each column against remaining columns 
        result = []
        for col in range(M-1):
            conv = irfft(X[:, [col]] * Y[:, col+1:], axis=0)
            corrs = np.roll(conv, shift, axis=0)[:window]
            np.mean(np.max(corrs, axis=0))
            result.extend()
        return result


######################
#
# Data filters
#
######################

def not_outlier(x: Any, method: str = 'iq10', bounds: bool = False) -> np.array:
    """Test if elements of x are not column-wise outliers

    Args:
        x: Input array to test element-wise
        method: method to filter, in {'iq{D}', 'tukey', 'farout'}
        bounds: If True, return (low, high) values of inlier range

    Returns:
        boolean array if element is not a column-wise outlier

    Notes:
    - 'iq{D}' - screen by iq range [Q2 +/- D times (Q3-Q1)]
    - 'tukey' -  [Q1 - 1.5(Q3-Q1), Q3 + 1.5(Q3-Q1)] 
    - 'farout' - tukey with 3IQ

    """
    def nancmp(f, a, b):
        valid = ~np.isnan(a)
        valid[valid] = f(a[valid] , b)
        return valid

    x = np.array(x)
    if len(x.shape) == 1:
        lb, median, ub = np.nanpercentile(x, [25, 50, 75])
        if method.lower().startswith(('tukey', 'far')):
            w = 1.5 if method[0].lower() == 't' else 3.0
            f = [lb - (w * (ub - lb)), ub + (w * (ub - lb))]
            if not bounds:
                f = (nancmp(np.greater_equal, x, f[0]) &
                     nancmp(np.less_equal, x, f[1]))
        elif method.lower().startswith('iq'):
            w = float(re.sub('\D', '', method))
            f = [median - (w * (ub - lb)), median + (w * (ub - lb))]
            if not bounds:
                f = (nancmp(np.greater_equal, x, f[0]) &
                     nancmp(np.less_equal, x, f[1]))
        else:
            raise Exception("outliers method not in ['iq[D]', 'tukey']")
        return np.array(f)
    else:
        return np.hstack([not_outlier(x[:, i],
                                      method=method,
                                      bounds=bounds).reshape(-1, 1)
                          for i in range(x.shape[1])])

from numpy.ma import masked_invalid as valid
def weighted_average(df: DataFrame, weights: str = '') -> Series:
    """Weighted means of data frame

    Args:
        df: DataFrame containing values, and optional weights, in columns
        weights: Column name to use as weights

    Returns:
        Series of weighted means

    Notes:
    - ignores NaN's using numpy.ma 
    """
    if not weights:
        cols = df.columns
    else:
        cols = df.columns.difference([weights])
        weights = df[weights].astype(float)
    return Series(np.ma.average(valid(df[cols].astype(float)),
                                weights=weights,
                                axis=0), index=cols)

def fractiles(values: Iterable, pct: Iterable, keys: Iterable | None = None, 
                ascending: bool = False) -> List[int]:
    """Sort and assign values into fractiles

    Args:
        values: input array to assign to fractiles
        pct: list of percentiles 0..100
        keys: key values to determine breakpoints, use values if None
        ascending: if True, assign to fractiles in ascending order
    
    Returns:
        list of fractile assignments {1,.., len(pct)} s.t. value <= pctile
    """
    if keys is None:
        keys = values
    keys = np.array(keys)[~np.isnan(keys)]  # drop nan
    bp = list(np.percentile(keys, sorted(pct))) + [np.inf]
    if ascending:
        return 1 + np.searchsorted(bp, values, side='left')
    else:
        return 1 + len(pct) - np.searchsorted(bp, values, side='left')


def winsorize(df, quantiles=[0.025, 0.975]):
    """Winsorise dataframe by column quantiles (default=[0.025, 0.975])

    Args:
        df: Input DataFrame
        quantiles: high and low fractions of distribution to truncate
    """
    lower = df.quantile(min(quantiles), interpolation='higher')
    upper = df.quantile(max(quantiles), interpolation='lower')
    if types.is_list_like(lower):
        return df.clip(lower=lower, upper=upper, axis=1)
    else:   # input was Series
        return df.clip(lower=lower, upper=upper)


def impute_em(X: np.ndarray, add_intercept: bool = True,
              tol: float = 1e-12, maxiter: int = 200,
              verbose: int = 1) -> Tuple[np.ndarray, DataFrame]:
    """Fill missing data with EM Normal distribution"""
    if add_intercept:
        X = np.hstack((np.ones((X.shape[0], 1)), X))
    missing = np.isnan(X)   # identify missing entries
    assert(not np.any(np.all(missing, axis=1)))    # no row all missing
    assert(not np.any(np.all(missing, axis=0)))    # no column all missing
    cols = np.flatnonzero(np.any(missing, axis=0)) # columns with missing 

    results = []
    for niter in range(maxiter+1):
        if not niter:
            # Initially, just replace with column means
            for col in cols: 
                X[missing[:, col], col] = np.nanmean(X[:, col])
        else:
            XX = X.T @ X
            inv_XX = inv(XX)
            for col in cols:  # E, M step for each column with missing values
                # "M" step: estimate covariance matrix
                mask = np.ones(X.shape[1], dtype=bool)
                mask[col] = 0
                # x = np.delete(X, (col), axis=1)
                if False:
                    #xx = np.delete(np.delete(XX, (col), axis=0), (col), axis=1)
                    M = inv(XX[:, mask][mask, :]) @ X[:, mask].T @ X[:, col]
                else:
                    M = -inv_XX[mask, col] / inv_XX[col, col]

                # "E" step: update expected missing values
                # y = X[:, mask] @ M
                X[missing[:, col], col] = X[missing[:, col],:][:, mask] @ M
        x = X[:, add_intercept:]
        # record the current NLL
        nll = -sum(multivariate_normal.logpdf(x,
                                              mean=np.mean(x, axis=0),
                                              cov=np.cov(x.T, bias=True),
                                              allow_singular=True))
        if verbose:
            print(f"{niter} {nll:.6f}")
        if niter and prev_nll - nll < tol:
            break
        prev_nll = nll
    return x

######################
#
# Econometrics
#
######################

def integration_order(df: Series, noprint: bool = True, max_order: int = 5,
                      pvalue: float = 0.05, lags: str | int = 'AIC') -> int:
    """Returns order of integration by iteratively testing for unit root

    Args:
        df: Input Series
        noprint: Whether to display results
        max_order: maximum number of orders to test
        pvalue: Required p-value to reject Dickey-Fuller unit root
        lags: Method automatically determine lag length, or maxlag;
              in {"AIC", "BIC", "t-stat"}, int (maxlag), 0 (12*(nobs/100)^{1/4})

    Returns:
        Integration order, or -1 if max_order exceeded
    """
    if not noprint:
        print("Augmented Dickey-Fuller unit root test:")
    for i in range(max_order):
        if not lags:
            dftest = adfuller(df, maxlag=None, autolag=None)
        elif isinstance(lags, str):
            dftest = adfuller(df, autolag=lags)
        else:
            dftest = adfuller(df, autolag=None, maxlag=lags)
        if not noprint:
            results = Series(dftest[0:4],
                             index=['Test Statistic',
                                    'p-value',
                                    'Lags Used',
                                    'Obs Used'],
                             name=f"I({i})")
            for k,v in dftest[4].items():
                results[f"Critical Value ({k})"] = v
            print(results.to_frame().T.to_string())
                
        if dftest[1] < pvalue:  # reject null that is a unit root
            return i
        df = df.diff().dropna()
    return -1

def least_squares(data: DataFrame, y: List[str] = ['y'],
                  x: List[str] = ['x'], add_constant: bool = True,
                  stdres: bool = False) -> Series | DataFrame:
    """To compute least square coefficients, supports groupby().apply

    Args:
        data: DataFrame with x and y series in columns
        x: List of x columns
        y: List of y columns
        add_constant: Whether to add intercept as first column
        stdres: Whether to output residual stdev

    Returns:
        DataFrame (multiple) or Series (simple) of regression coefficients

    """
    X = data[x].to_numpy()
    Y = data[y].to_numpy()
    if add_constant:
        X = np.hstack([np.ones((X.shape[0], 1)), X])
        x = ['Intercept'] + x
    b = np.dot(np.linalg.inv(np.dot(X.T, X)), np.dot(X.T, Y)).T
    if stdres:
        b = np.hstack([b, np.std(Y-(X @ b.T), axis=0).reshape(-1,1)])
        x = x + ['stdres']
    return (DataFrame(b, columns=x, index=y) if len(b) > 1 else
            Series(b[0], x))   # return as Series for groupby.apply

def fstats(x: Series | np.ndarray, tail: float = 0.15) -> np.ndarray:
    """Helper to compute F-stats at all candidate break points
    
    Args:
        x: Input Series
        tail: Tail fractions to skip computations

    Returns:
        Array of f-stats at each candidate break-point
    """
    n = len(x)
    rse = np.array(np.var(x, ddof=0))
    sse = np.ones(n) * rse
    for i in range(int(n * tail), int((1-tail) * n)+1):
        sse[i] = (np.var(x[:i], ddof=0)*i + np.var(x[i:], ddof=0)*(n-i))/n
    return ((n-2)/2) * (rse - sse)/rse


from collections import namedtuple    
def lm(x: np.ndarray | DataFrame | Series, y: np.ndarray | DataFrame | Series,
       add_constant: bool = True, flatten: bool = True) -> NamedTuple:
    """Calculate linear multiple regression model results as namedtuple

    Args:
        x: RHS independent variables
        y: LHS dependent variables
        add_constant: Whether to hstack 'Intercept' column before x variables
        flatten: Whether to flatten fitted and residuals series

    Returns:
        LinearModel named tuple, with key and values

        - coefficients: estimated linear regression coefficients
        - fitted: fitted y values
        - residuals: fitted - actual y values
        - rsq: R-squared
        - rvalue: square root of r-squared
        - stderr: std dev of residuals
    """
    
    def f(a):
        """helper to optionally flatten 1D array"""
        if not flatten or not isinstance(a, np.ndarray):
            return a
        if len(a.shape) == 1 or a.shape[1] == 1:
            return float(a) if a.shape[0] == 1 else a.flatten()
        return a.flatten() if a.shape[0] == 1 else a
    
    X = np.array(x)
    Y = np.array(y)
    if len(X.shape) == 1 or X.shape[0]==1:
        X = X.reshape((-1,1))
    if len(Y.shape) == 1 or Y.shape[0]==1:
        Y = Y.reshape((-1,1))
    if add_constant:
        X = np.hstack([np.ones((X.shape[0], 1)), X])
    b = np.dot(np.linalg.inv(np.dot(X.T, X)), np.dot(X.T, Y))
    out = {'coefficients': f(b)}
    out['fitted'] = f(X @ b)
    out['residuals'] = f(Y) - out['fitted']
    out['rsq'] = f(np.var(out['fitted'], axis=0)) / f(np.var(Y, axis=0))
    out['rvalue'] = f(np.sqrt(out['rsq']))
    out['stderr'] = f(np.std(out['residuals'], axis=0))
    return namedtuple('LinearModel', out.keys())(**out)

######################
#
# Finance
#
######################

class Volatility:
    """Class of static methods to compute intra-day volatility measures"""
    
    def HL(high: DataFrame, low: DataFrame,
           last: DataFrame = None) -> DataFrame:
        """Compute Parkinson volatility from high and low prices
    
        Args:
          high: DataFrame of high prices (observations x stocks)
          low: DataFrame of low prices (observations x stocks)
          last: DataFrame of last prices, for forward filling if high low missing

        Returns:
          Estimated volatility
        """
        if last is not None:
            high = high.where(high.notna(), last.shift())
            low = low.where(high.notna(), last.shift())
        return np.sqrt((np.log(high / low)**2).mean(axis=0, skipna=True)
                       / (4 * np.log(2)))

    def OHLC(first: DataFrame, high: DataFrame, low: DataFrame,
             last: DataFrame, ffill: bool = False,
             zero_mean: bool = True) -> DataFrame:
        """Compute Garman-Klass or Rogers-Satchell (non zero mean) OHLC vol
    
        Args:
          first: DataFrame of open prices (observations x stocks)
          high: DataFrame of high prices (observations x stocks)
          low: DataFrame of low prices (observations x stocks)
          last: DataFrame of close prices (observations x stocks)

        Returns:
          Estimated volatility 
        """
        if ffill:
            last = last.ffill()
            high = high.where(high.notna(), last.shift())
            low = low.where(low.notna(), last.shift())
            first = low.where(first.notna(), last.shift())
        if zero_mean:  # Garman-Klass (assuming zero mean drift)
            v = ((np.log(high / low)**2) / 2
                 - (2*np.log(2) - 1) * (np.log(last / first)**2))\
                 .mean(axis=0, skipna=True)
        else:          # Rogers-Satchell (non zero mean drift)
            v = ((np.log(high / close) * np.log(high / close))
                 + (np.log(high / close) * np.log(high / close)))\
                 .mean(axis=0, skipna=True)
        return np.sqrt(v)
    

def maximum_drawdown(x: Series, is_price_level: bool = False) -> Series:
    """Compute max drawdown (max loss from previous high) period and returns

    Args:
        x: Returns or price level series
        is_price_level: Whether input are price index levels, or returns

    Returns:
        Series with start and end levels, keyed by dates, of maximum drawdown

    Notes:
        MDD = (Trough - Peak) / Peak
    """
    if is_price_level:
        cumsum = np.log(x)
    else:
        cumsum = np.log(1 + x).cumsum()
    cummax = cumsum.cummax()
    end = (cummax - cumsum).idxmax()
    beg = cumsum[cumsum.index <= end].idxmax()
    dd = cumsum.loc[[beg, end]]
    return np.exp(dd)

# proportion of failures likelihood test
def kupiecLR(s: int, n: int, var: float = 0.95) -> Dict[str, float]:
    """Kupiec Likelihood Ratio test (S violations in N trials) of VaR

    Args:
        s: number of violations
        n: number of observations
        var: VaR level (e.g. 0.95)

    Returns:
        Dictionary of likelihood statistic and pvalue
    """
    
    p = 1 - var        # e.g. var95 is 0.95
    t = n - s          # number of non-violations
    num = np.log(1 - p)*(n - s) + np.log(p)*s
    den = np.log(1 - (s/n))*(n - s) + np.log(s/n)*s
    lr = -2 * (num - den)
    return {'statistic': lr, 'pvalue': 1 - chi2.cdf(lr, df=1)}


def pof(X: Series, pred: Series | float, var: float = 0.95) -> Dict[str, float]:
    """Kupiec proportion of failures VaR test

    Args:
        X: Observed Series
        pred: Predicted standard deviation
        var: VaR level (e.g. 0.95)

    Returns:
        Dictionary {'statistics', 'pvalue', 's': violations, 'n': observations}
    """

    Z = X / pred
    z = norm.ppf(1 - var)
    r = {'n': len(Z), 's': np.sum(Z < z)}
    r.update(kupiecLR(r['s'], r['n'], var))
    return r

# convert alpha to halflife
from pandas.api import types
def halflife(alpha):
    """Returns halflife from alpha = -ln(2)/ln(lambda), where lambda=1-alpha"""
    if types.is_list_like(alpha):
        return [halflife(a) for a in alpha]
    if 0 < alpha < 1: 
        return -np.log(2)/np.log(1-alpha)
    else:
        return np.inf if (alpha > 0) else 0

class RiskMeasure:
    """Class to compute risk measures for a time series
    Args:
        x: Time series of observations
        alpha: Risk tolerance threshold (default is 0.95 for 5% tail)
    """
    def __init__(self, x: Series, alpha: float = 0.95):
        self.x = x
        self.alpha = alpha
        
    def expected_shortfall(self, normal: bool = False):
        """Return value at risk: empirical or normal assumption"""
        if normal:
            return (-np.std(self.x) * norm.pdf(norm.ppf(1 - self.alpha))
                    / (1 - self.alpha))
        else:
            return np.mean(self.x[self.x < self.value_at_risk()])
            
    def value_at_risk(self, normal: bool = False):
        """Return value at risk: empirical or normal assumption"""
        if normal:
            return np.std(self.x) * norm.ppf(1 - self.alpha)
        else:
            return np.percentile(self.x, 100 * (1 - self.alpha))
        

# helper methods for basic bond math calculations
class Interest:
    
    @staticmethod
    def present_value(flow: float, n: float, spot: float) -> float:
        """Present Value of a cash flow at n period, given spot interest rate

        Args:
           flow: Amount of future cash flow
            n: Number of periods to discount
            spot: Interest rate per period

        Returns:
           PV of cash flow discounted by spot rate compounded over n periods
        """
        return flow / ((1 + spot) ** n)


    @staticmethod
    def weighted_maturity(flows: List[float], spot: float, first: int = 1,
                          returned: bool = False) -> float:
        """Average maturity weighted by PV of flows discounted by spot rate 

        Args:
            flows: List of cash flow amounts at each future period
            spot: Interest rate per period
            first: First period when cash flows begin
            returned: If `True`, the tuple (`average`, `sum_of_weights`)
                is returned, otherwise only the average is returned.

        Returns:
            Weighted average maturity of future cash flows
        """
        v = [Interest.present_value(flow=flow,
                                    n=n + first,
                                    spot=rate)
             for n, (flow, rate) in enumerate(zip(flows, spot))]
        return np.average(np.arange(len(v)) + first, weights=v, returned=returned)


    @staticmethod
    def par_duration(nominal: float, n: int, face: float = 1.,
                     m: int = 1, first: float = 1.) -> float:
        """Macaulay duration of a coupon bond, currently selling at par price

        Args:
            nominal: Nominal annual coupon rate of the bond
            n: Number of years till maturity
            face: Face value of bond to be returned at maturity
            m: Number of intra-year coupon payments
            first: First year when cash flows begin

        Returns:
            Macaulay duration of a par coupon bond

        Notes:
            Assumes bond currently selling at par
        """
        coupon = nominal * face     # assume par bond
        flows = [coupon / m] * (n * m - 1) + [face + coupon / m]  # face in last
        d, v = Interest.weighted_maturity(flows,
                                          spot=[nominal / m] * (n * m),
                                          first=first * m,
                                          returned=True)
        return d / m

    @staticmethod
    def discounted_cash_flow(flows: float | List[float],
                             spot: float | List[float],
                             first: int = 1) -> float:
        """PV of future cash flows, starting at first period

        Args:
            flows: Amounts of future annual cash flows
            spot: Interest rate, or rates, per year
            first: First period when cash flow begins

        Returns:
            Discounter present value of future cash flows
        """

        if not types.is_list_like(flows):    # flows can be different each period
            flows = [flows]                  # else assume same flow every period
            if not types.is_list_like(spot): # spot can be different per flow
                spot = [spot]                # else use same spot each period
        if len(flows) == 1:
            flows = list(flows) * len(spot)  # flows to be same length as spot
        if len(spot) == 1:
            spot = list(spot) * len(flows)   # spot to be same length as flows
        return np.sum([Interest.present_value(flow=flow,
                                              n=first + n,
                                              spot=rate)
                       for n, (flow, rate) in enumerate(zip(flows, spot))])


    @staticmethod
    def forward_rates(spot: List[float], base=0) -> List[float]:
        """Forward rates implied by spot rates starting after base periods

        Args:
            spot: List of current annual spot interest rates at each period
            base: Base periods skipped by initial spot rate in input list

        Returns:
            List of forward curve annual rates
        """
        return [(((1 + num)**(n + 1 + base) / (1 + den)**(n + base)) - 1)
                for n, (num, den) in enumerate(zip(spot, [0] + list(spot[:-1])))]


    @staticmethod
    def bootstrap_rates(ytm: float, nominal: List[float], m: int = 1) -> float:
        """Nominal rate to maturity of par bond from ytm and sequence of nominals

        Args:
            ytm: Annualized yield to maturity of par bond
            nominal: Annualized spot rates each period (excl last maturity period)
            m: Number of periods per year

        Returns:
            Nominal annualized effective interest rate till maturity

        Notes:
            Assumes bond currently selling at par
        """
        n = len(nominal) + 1       # implicit number of coupons through maturity
        spot = [r / m for r in nominal]  # spot rate per period
        pv = (1 - Interest.discounted_cash_flow(flows=ytm/m, spot=spot))
        return (((1 + (ytm / m)) / pv)**(1 / n) - 1) * m


from cvxopt import matrix, solvers
def quadprog(sigma):
    """Quadratic solver for portfolio optimization"""
    G = matrix(np.diag([-1.]*sigma.shape[1]))
    A = matrix(np.ones((1, sigma.shape[1])))
    b = matrix(np.ones((1, 1)))
    h = matrix(np.zeros((sigma.shape[1], 1)))
    sol = solvers.qp(P=matrix(sigma), q=h, G=G, h=h, A=A, b=b,
                     options=dict(show_progress=False))
    x = np.array(sol['x']).ravel()
    return x

    
if __name__=="__main__":
    # Verify with Jorion Chapter 5 Solution
    ytm = list(np.arange(0.0525, 0.1025, 0.0025))
    spot = np.array([])
    for y in ytm:
        spot = np.append(spot, Interest.bootstrap_rates(y, nominal=spot, m=2))
    jorion_sol5 = [.0797,.0827,.0859,.0892,.0925,.0961,.0997,.1036,.1077,.112]
    assert np.allclose(jorion_sol5, spot[-len(jorion_sol5):], atol=0.0001)
    print(spot)



