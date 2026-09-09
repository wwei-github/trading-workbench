import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'

dayjs.extend(utc)
dayjs.extend(timezone)

export { dayjs }
export const BJ = 'Asia/Shanghai'

/** 将后端返回的 UTC naive datetime 转为北京时间 dayjs 对象 */
export const bj = (v?: string | null) => dayjs.utc(v).tz(BJ)
