/* 字幕呈现格式：保留原文，只对预览和导出转换明确的数字。 */
(() => {
  const digits = {零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9};
  const small = {十: 10n, 百: 100n, 千: 1000n};
  const chinese = '零一二三四五六七八九';
  const quantifier = /^[个位名人岁年月日号天时分秒点元块角毛米里斤克吨升毫厘公万亿件本张集章节回次倍成折楼层度台只口家套辆枚瓶箱包支份笔页段周晚早万千百十%％]/;
  const protectedPhrases = /一心一意|一模一样|一言不发|一无所有|一清二楚|三心二意|三三两两|七上八下|乱七八糟|五花八门|千方百计|百发百中|一石二鸟|万无一失|万一|一起|一样|一定|一些|一般|一直|一会儿|一边|一切|统一|唯一|之一/g;

  function sectionText(number) {
    let out = '', zero = false;
    for (let position = 3; position >= 0; position--) {
      const value = Math.floor(number / 10 ** position) % 10;
      if (!value) { if (out) zero = true; continue; }
      if (zero) out += '零';
      out += chinese[value] + ['', '十', '百', '千'][position]; zero = false;
    }
    return out;
  }

  function integerToChinese(text, digitwise = false) {
    if (digitwise || (text.length > 1 && text[0] === '0') || text.length > 16) return [...text].map(n => n === '0' ? '〇' : chinese[Number(n)]).join('');
    if (/^0+$/.test(text)) return '零';
    const groups = [];
    for (let end = text.length; end > 0; end -= 4) groups.push(Number(text.slice(Math.max(0, end - 4), end)));
    const units = ['', '万', '亿', '万亿'];
    let out = '', zero = false;
    for (let i = groups.length - 1; i >= 0; i--) {
      const value = groups[i];
      if (!value) { if (out) zero = true; continue; }
      if (out && (zero || value < 1000)) out += '零';
      out += sectionText(value) + units[i]; zero = false;
    }
    return out.replace(/^一十/, '十');
  }

  function toChinese(text) {
    return text.replace(/[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?/g, (matched, offset, whole) => {
      const signed = /^[+-]/.test(matched);
      const number = signed ? matched.slice(1) : matched;
      const start = offset + (signed ? 1 : 0);
      const before = whole[start - 1] || '', after = whole.slice(start + number.length);
      const prevChar = whole[offset - 1] || '';
      if (/[A-Za-z_/#@]/.test(before) || /^[A-Za-z_]/.test(after)
          || (before === '.' && /[A-Za-z0-9]/.test(whole[start - 2] || '')) || /^\.\d/.test(after)) return matched;
      // 时间、日期和比例里的数字保持原样：3:30 不能变成“三:三十”，2024-01-01 不能变成汉字纪年。
      const integer = number.split('.')[0];
      const datePart = integer.length === 4 || /^0\d/.test(integer);
      if (/[:：]/.test(before) || /^[:：]/.test(after)
          || ((before === '-' || before === '/') && datePart)
          || ((/^-/.test(after) || /^\//.test(after)) && datePart)
          || (before === '/' || /^\//.test(after))) return matched;
      // 数字前面的 - 只有不在数字/汉字后面时才是负号，否则是日期或范围分隔符。
      const separator = signed && /[\dA-Za-z一-鿿]/.test(prevChar);
      const sign = signed && !separator ? (matched[0] === '-' ? '负' : '正') : '';
      const parts = number.replaceAll(',', '').split('.');
      const prefix = whole.slice(0, start);
      const digitwise = (parts[0].length === 4 && /^年/.test(after))
        || /(?:电话|手机|号码|编号|密码|身份证)[：:\s]*$/.test(prefix)
        || (parts[0].length === 11 && /^1/.test(parts[0]) && !quantifier.test(after));
      return (separator ? matched[0] : '') + sign + integerToChinese(parts[0], digitwise)
        + (parts[1] ? '点' + [...parts[1]].map(n => chinese[Number(n)]).join('') : '');
    });
  }

  function integerToArabic(text) {
    // “一两”“两三个”是约数，不能按逐位读法变成 12、23。
    if (text.length > 1 && text.includes('两') && !/[十百千万亿]/.test(text)) return null;
    if ([...text].every(ch => ch in digits)) return [...text].map(ch => digits[ch]).join('');
    // 一百二、一千三等口语缩略有歧义，保留供人工确认。
    if (!/[零〇]/.test(text) && /[百千万亿][一二两三四五六七八九]$/.test(text)) return null;
    let total = 0n, section = 0n, number = 0n;
    for (const ch of text) {
      if (ch in digits) number = BigInt(digits[ch]);
      else if (ch in small) { section += (number || 1n) * small[ch]; number = 0n; }
      else if (ch === '万') { total += (section + number) * 10000n; section = 0n; number = 0n; }
      else if (ch === '亿') { total = (total + section + number) * 100000000n; section = 0n; number = 0n; }
    }
    return String(total + section + number);
  }

  function toArabic(text) {
    const protectedRanges = [...text.matchAll(protectedPhrases)].map(match => [match.index, match.index + match[0].length]);
    return text.replace(/[负正]?[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?/g, (matched, offset, whole) => {
      if (protectedRanges.some(([start, end]) => offset < end && offset + matched.length > start)) return matched;
      const after = whole.slice(offset + matched.length), before = whole.slice(0, offset);
      const sign = matched[0] === '负' ? '-' : matched[0] === '正' ? '+' : '';
      const number = matched.replace(/^[负正]/, '');
      if (/^[百千万亿]/.test(number)) return matched;
      const hasContext = quantifier.test(after) || /第$|(?:编号|号码|电话|手机|数字|密码|金额|数量|余额|售价|共计|合计)[：:\s]*$/.test(before);
      const isolated = (offset === 0 || /[\s，。！？：:（(“「]/.test(whole[offset - 1])) && (!after || /^[\s，。！？；;）)”」]/.test(after));
      if (!hasContext && !isolated) return matched;
      const parts = number.split('点'), integer = integerToArabic(parts[0]);
      if (integer == null) return matched;
      const separator = parts[1] && /^分(?!钟)/.test(after) ? '点' : '.';
      return sign + integer + (parts[1] ? separator + [...parts[1]].map(ch => digits[ch]).join('') : '');
    });
  }

  window.SubtitleNumbers = {format(text, arabic = false) {return arabic ? toArabic(String(text ?? '')) : toChinese(String(text ?? ''));}};
})();
