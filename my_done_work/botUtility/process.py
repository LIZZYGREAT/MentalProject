import re

class fulcategory:
    def __init__(self,name,parent,level):
        self.name = name
        self.children = dict()
        self.tot_score = None
        self.ful_score = None
        self.is_complete = None
        self.parent=parent
        self.level = level
    def gather_course(self,name,is_comple):
        result = set()
        if name in self.name:
            for item in self.children.values():
                if isinstance(item, fulcourse) and item.is_completed() == is_comple:
                    result.add(item.name)
                elif isinstance(item, fulcategory):
                    result.update(item.gather_course('',is_comple))
        else:
            for item in self.children.values():
                if isinstance(item, fulcategory):
                    result.update(item.gather_course(name,is_comple))
        return result
    def __repr__(self,indent=0):
        buffer = indent*'\t'+self.name
        if self.ful_score:
            buffer += f' 已修{self.ful_score}学分'
        if self.tot_score:
            buffer += f' 共{self.tot_score}学分'
        if self.is_complete:
            if self.is_complete == '是':
                buffer += f' 已完成'
            else:
                buffer += f' {self.is_complete}'
        if self.children:buffer += '\n'
        buffer += '\n'.join(i.__repr__(indent=indent+1) for i in self.children.values())
        return buffer
    def repr(self,indent=0):
        buffer = indent*'\t'+self.name
        if self.ful_score:
            buffer += f' 已修{self.ful_score}学分'
        if self.tot_score:
            buffer += f' 要求{self.tot_score}学分'
        if self.is_complete:
            if self.is_complete == '是':
                buffer += f' 已完成'
            else:
                buffer += f' {self.is_complete}'
        if self.children:buffer += '\n'
        buffer += '\n'.join(i.repr(indent=indent+1) for i in self.children.values())
        return buffer

code_pattern = re.compile(r'\w{4}\d{4}')
class fulcourse:
    def __init__(self,contents):
        assert len(contents) >= 7
        self.code, self.name, self.credit, self.fulcredit, self.score, self.is_comple, self.remark, *_ = contents
        if self.name.split(' ')[0].isdigit():
            self.name = self.name.split(' ',1)[1]
    def __repr__(self,indent=0):
        #buffer = f'{self.code} {self.name} 学分:{self.credit}'
        buffer = f'{self.name}'
        if self.fulcredit != self.credit:
            buffer += f' {self.fulcredit}/{self.credit}学分'
        else:
            buffer += f' {self.credit}学分'
        if '--' not in self.score:
            buffer += f' 得分:{self.score}'
        if self.is_comple == '是':
            buffer += ' 已通过'
        else:
            buffer += ' 未修'
        if self.remark:
            buffer += f' {self.remark}'
            if code_pattern.search(self.remark):
                buffer += '替代课程'
        return buffer
    def is_completed(self):
        return self.is_comple == '是'
    def repr(self, indent=0):
        return '\t'*indent + self.__repr__()

level1 = '一二三四五六七八九十'
level2 = tuple('('+i+')' for i in level1)
level3 = tuple(str(i) for i in range(10))
def get_level(string):
    for char in level1:
        if string.startswith(char):
            return 1
    for char in level2:
        if string.startswith(char):
            return 2
    for char in level3:
        if string.startswith(char):
            return 3
    return -1
def get_type(string):
    if code_pattern.search(string):
        return 'CLASS'
    elif get_level(string) != -1:
        return 'CAT'
    else:
        return 'UNK'

def is_category(string):
    return string.rstrip().lstrip() in ('通识必修课','通识选修课','专业必修课','专业选修课','大类基础课程','国际学分')

code_pattern = re.compile(r'\w{4}\d{4}')
def is_course_code(string):
    return code_pattern.match(string.rstrip().lstrip())

def is_numeric(string):
    try:
        float(string)
    except ValueError:
        return False
    return True

def is_tot(string):
    return '学分小计' in string or '应修学分' in string

class category:
    def __init__(self,name):
        self.name = name
        self.children = dict()
        self.tot_score = None
        self.remark = None
    def __repr__(self):
        buffer = self.name
        if self.tot_score:
            buffer += f' 共{self.tot_score}学分'
        if self.remark:
            buffer += f' {self.remark}'
        buffer += '\n'
        buffer += '\n'.join(i.__repr__() for i in self.children.values())
        return buffer
    def gather_course(self,name):
        result = set()
        if name in self.name:
            for item in self.children.values():
                if isinstance(item, course):
                    result.add(item.name)
                elif isinstance(item, category):
                    result.update(item.gather_course(''))
        else:
            for item in self.children.values():
                if isinstance(item, category):
                    result.update(item.gather_course(name))
        return result
    def repr(self,indent=0):
        buffer = indent*'\t'+self.name
        if self.tot_score:
            buffer += f' 共{self.tot_score}学分'
        if self.remark:
            buffer += f' {self.remark}'
        if self.children:buffer += '\n'
        buffer += '\n'.join(i.repr(indent=indent+1) for i in self.children.values())
        return buffer
        
class course:
    def __init__(self,contents):
        assert len(contents) >= 8
        self.code, self.name, self.credit, self.semester, self.sug_semester, self.is_must, self.depart, self.remark, *_ = contents
        if self.name.split(' ')[0].isdigit():
            self.name = self.name.split(' ',1)[1]
    def __repr__(self):
        #buffer = f'{self.code} {self.name} 学分:{self.credit}'
        buffer = f'{self.name} {self.credit}学分'
        if self.semester:
            buffer += f' {self.semester}学期'#开课'
        if self.sug_semester:
            buffer += f' 建议{self.sug_semester}学期选'
        if self.is_must == '是':
            buffer += ' 必修'
        elif self.is_must == '不是':
            buffer += ' 选修'
        if self.depart:
            buffer += f' {self.depart}'#开课'
        if self.remark:
            buffer += f' {self.remark}'
        return buffer
    def repr(self, indent=0):
        return '\t'*indent + self.__repr__()

replacement = dict()
replacement['数学科学学院'] = '数院'
replacement['统计与数据科学学院'] = '统院'
replacement['计算机学院'] = '计院'
replacement['网络空间安全学院'] = '网安'
replacement['马克思主义基础理论教学部'] = '马主义教学部'
def common_replaces(string):
    for key,item in replacement.items():
        string = string.replace(key,item)
    return string

def process(comPlan,allPlan):
    result = dict()
    temp_str = allPlan[:comPlan.find("个人替代课程")]
    root_cat = category('总计')
    buffer = ''
    for line in temp_str.split('\n'):
        if len(line) <= 4: continue
        if '|' not in line:
            buffer += line + '\n'
        else:
            res = line.split('|')
            if '全程总计' in line:
                root_cat.tot_score = float(line.split('|')[1])
            elif '备注' in line:
                pass
            else:
                cur = root_cat
                contents = tuple(map(lambda x:x.lstrip().rstrip(),line.split('|')))
                if not is_category(contents[0]):
                    continue
                headers = []
                for idx,i in enumerate(contents):
                    if i and not is_course_code(i) and not is_numeric(i) and not is_tot(i):
                        headers.append(i)
                        if i not in cur.children:
                            cur.children[i] = category(i)
                        cur = cur.children[i]
                    else:
                        if is_course_code(i):
                            temp = course(contents[idx:])
                            cur.children[temp.name] = temp
                            break
                        elif is_tot(i):
                            i = contents[idx+1]
                        if is_numeric(i):
                            cur.tot_score = float(i)
                        break
    result['allPlan'] = buffer + common_replaces(root_cat.repr())
    codes = 'AECDB'
    for idx,type_name in enumerate(('通识必修课','通识选修课','专业必修课','专业选修课','大类基础课程')):
        result[f'{codes[idx]}0'] = root_cat.gather_course(type_name)
    
    buffer = ''
    cur_cat = fulcategory('总计',None,0)
    origin = cur_cat
    for line in comPlan.split('\n'):
        if not line or '---' in line or '是否通过' in line:
            continue
        line_type = get_type(line)
        if line_type == 'UNK':
            buffer += line.replace('|','')+'\n'
        elif line_type == 'CLASS':
            _,*items = map(lambda x:x.lstrip().rstrip(), line.split('|'))
            cur_cat.children[items[1]] = fulcourse(items)
        elif line_type == 'CAT':
            name, all_sco, get_sco, _, status, *_ = map(lambda x:x.lstrip().rstrip(), line.split('|'))
            new_level = get_level(line)
            while new_level <= cur_cat.level:
                cur_cat = cur_cat.parent
            cur_cat.children[name] = fulcategory(name,cur_cat,new_level)
            cur_cat = cur_cat.children[name]
            cur_cat.tot_score = all_sco
            cur_cat.ful_score = get_sco
            cur_cat.is_complete = status

    for idx,type_name in enumerate(('通识必修课','通识选修课','专业必修课','专业选修课','大类基础课程')):
        result[f'{codes[idx]}1'] = origin.gather_course(type_name,True)
        result[f'{codes[idx]}0'].update(origin.gather_course(type_name,False))
        result[f'{codes[idx]}0'] -= result[f'{codes[idx]}1']
    for key,val in result.items():
        if isinstance(val,set):
            result[key] = tuple(val)
    result['comPlan'] = buffer+origin.repr()
    return result
