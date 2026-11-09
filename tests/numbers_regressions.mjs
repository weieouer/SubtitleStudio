import vm from 'node:vm';
import assert from 'node:assert/strict';

export function runNumberTests(source) {
  const context = vm.createContext({window: {}});
  vm.runInContext(source, context);
  const format = context.window.SubtitleNumbers.format;
  const cases = [
    ['第12集，3个人。', false, '第十二集，三个人。'],
    ['第十二集，三个人。', true, '第12集，3个人。'],
    ['一百二十三元', true, '123元'], ['123元', false, '一百二十三元'],
    ['一万零二十元', true, '10020元'], ['10020元', false, '一万零二十元'],
    ['二〇二六年', true, '2026年'], ['2026年', false, '二〇二六年'],
    ['负三点五米', true, '-3.5米'], ['-3.5米', false, '负三点五米'],
    ['零点零零五秒', true, '0.005秒'], ['0.005秒', false, '零点零零五秒'],
    ['三点五分', true, '3点5分'], ['三点五分钟', true, '3.5分钟'],
    ['电话一三八〇〇一三八〇〇〇', true, '电话13800138000'],
    ['电话13800138000', false, '电话一三八〇〇一三八〇〇〇'],
    ['编号0012', false, '编号〇〇一二'], ['编号〇〇一二', true, '编号0012'],
    ['共计两百元', true, '共计200元'], ['十二', true, '12'], ['0', false, '零'],
    ['王一博在三体里，一心一意，一起走。', true, '王一博在三体里，一心一意，一起走。'],
    ['万一有人问，三三两两地走。', true, '万一有人问，三三两两地走。'],
    ['一百二元', true, '一百二元'],
    ['AIGC2026和DJ004_001、H.264、3D', false, 'AIGC2026和DJ004_001、H.264、3D'],
    ['一亿元', true, '100000000元'], ['100000000元', false, '一亿元'],
    ['一万亿元', true, '1000000000000元'], ['1000000000000元', false, '一万亿元'],
    ['售价1,234.50元', false, '售价一千二百三十四点五零元'],
    // 约数不能按逐位读成两位数。
    ['他说一两就够了', true, '他说一两就够了'],
    ['两三个人一起走', true, '两三个人一起走'],
    // 时间、日期、比例中的数字不转成汉字。
    ['3:30开始', false, '3:30开始'],
    ['2024-01-01开机', false, '2024-01-01开机'],
    ['比分3/5', false, '比分3/5'],
  ];
  const results = cases.map(([input, arabic, expected], index) => {
    assert.equal(format(input, arabic), expected, input);
    return {name: 'number_format_' + (index + 1), input, arabic, expected, passed: true};
  });
  return {total: results.length, all_passed: true, results};
}
