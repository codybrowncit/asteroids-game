from bottle import get, post, run, request, response
from bottle import redirect, debug, static_file
import xmlrpclib
import template

DB_ADDRESS = 'http://youface.cs.dixie.edu/'
DB_SERVER = None

title = 'YouFace'
subtitle = "A bazillion dollars and it's yours!"
links = [
    { 'href': 'http://cit.cs.dixie.edu/cs/cs1410/',     'text': 'CS 1410' },
    { 'href': 'http://codrilla.cs.dixie.edu/',          'text': 'Codrilla' },
    { 'href': 'http://new.dixie.edu/reg/syllabus/',     'text': 'College calendar' },
]

@get('/youface.css')
def stylesheet():
    return static_file('youface.css', root='./')

@get('/loginscreen')
def loginscreen():
##    return 'Hello'
    f = open('login-page.template','r')
    lines = f.read()
    t= template.Template(lines)
    data= {'title':title,
           'subtitle':subtitle,
           'links':links}
    return t.render(data)

@post('/login')
def login():
##    return 'Does it work?'
    name = request.forms.get('name')
    password = request.forms.get('password')
    type1 = request.forms.get('type')
    s=name+password+type1
    
    response.set_cookie('name', name, path='/')
    response.set_cookie('password', password, path='/')
    redirect('/')
##    return s

@get('/')
def url():
    name= request.COOKIES.get('name', '')
    password= request.COOKIES.get('password', '')
    return name, password

@get('/logout')
def logout():
    response.set_cookie('name', '', path='/')
    response.set_cookie('password', '', path='/')
    redirect('/')  

def main():
    global DB_SERVER, DB_ADDRESS

    print 'Using YouFace server at', DB_ADDRESS
    DB_SERVER = xmlrpclib.ServerProxy(DB_ADDRESS, allow_none=True)
    debug(True)
    run(host='localhost', port=8080, reloader=True)

if __name__ == '__main__':
    main()
