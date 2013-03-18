#! /usr/bin/python
# -*- coding=utf-8 -*-

import os
import sys
import gtk
import gobject
import urlparse
import ConfigParser
import aptsources.distro
import aptsources.distinfo
from aptsources.sourceslist import SourcesList
import gettext
import thread
import pycurl
import cStringIO
from CountryInformation import CountryInformation
import commands

gettext.install("mintsources", "/usr/share/linuxmint/locale")

# i18n for menu item
menuName = _("Software Sources")
menuComment = _("Configure the sources for installable software and updates")

SPEED_PIX_WIDTH = 125
SPEED_PIX_HEIGHT = 16

class Component():
    def __init__(self, name, description, selected):
        self.name = name
        self.description = description
        self.selected = selected
        self.widget = None

    def set_widget(self, widget):
        self.widget = widget

class Mirror():
    def __init__(self, url, country_code):
        self.url = url
        self.country_code = country_code                

class ComponentToggleCheckBox(gtk.CheckButton):
    def __init__(self, application, component):
        self.component = component        
        gtk.CheckButton.__init__(self, self.component.description)
        self.set_active(component.selected)                    
        self.connect("toggled", self._on_toggled)
    
    def _on_toggled(self, widget):
        self.component.selected = widget.get_active()        

class ServerSelectionComboBox(gtk.ComboBox):
    def __init__(self, application, repo):
        gtk.ComboBox.__init__(self)
        
        self._repo = repo
        self._application = application
        
        self._model = gtk.ListStore(str, str, bool, bool)
        self.set_model(self._model)
        
        cell = gtk.CellRendererText()
        self.pack_start(cell, True)
        self.add_attribute(cell, 'text', 0)
        
        self.set_row_separator_func(lambda m,i: m.get(i, 3)[0])
        
        self.refresh()
        
        self._block_on_changed = False
        self.connect("changed", self._on_changed)
    
    def _on_changed(self, widget):
        if self._block_on_changed:
            return
        url = self._model[widget.get_active()][1]
        if url == None:
            url = self._application.mirror_selection_dialog.run(self._repo)
        print url
        if url != None:
            self._repo["distro"].main_server = url
            self._repo["distro"].change_server(url)
            self._application.save_sourceslist()
            self._repo["distro"].get_sources(self._application.sourceslist)
        self.refresh()
    
    def refresh(self):
        self._block_on_changed = True
        self._model.clear()
        selected_iter = None
        for name, url, active in self._repo["distro"].get_server_list():
            tree_iter = self._model.append((name, url, active, False))
            if active:
                selected_iter = tree_iter
        self._model.append((None, None, None, True))
        self._model.append((_("Other..."), None, None, False))
        
        if selected_iter is not None:
            self.set_active_iter(selected_iter)
        
        self._block_on_changed = False

class MirrorSelectionDialog(object):
    MIRROR_COLUMN = 0
    MIRROR_URL_COLUMN = 1
    MIRROR_COUNTRY_COLUMN = 2
    MIRROR_SPEED_COLUMN = 3
    MIRROR_SPEED_BAR_COLUMN = 4
    def __init__(self, application, ui_builder):
        self._application = application
        self._ui_builder = ui_builder
        
        self._dialog = ui_builder.get_object("mirror_selection_dialog")
        self._dialog.set_transient_for(application._main_window)
        
        self._mirrors = None
        self._mirrors_model = gtk.ListStore(object, str, gtk.gdk.Pixbuf, float, gtk.gdk.Pixbuf)
        self._treeview = ui_builder.get_object("mirrors_treeview")
        self._treeview.set_model(self._mirrors_model)
        self._treeview.set_headers_clickable(True)
        
        self._mirrors_model.set_sort_column_id(MirrorSelectionDialog.MIRROR_SPEED_COLUMN, gtk.SORT_DESCENDING)
        
        r = gtk.CellRendererPixbuf()
        col = gtk.TreeViewColumn(_("Country"), r, pixbuf = MirrorSelectionDialog.MIRROR_COUNTRY_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_COUNTRY_COLUMN)

        r = gtk.CellRendererText()
        col = gtk.TreeViewColumn(_("URL"), r, text = MirrorSelectionDialog.MIRROR_URL_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_URL_COLUMN)            
        
        r = gtk.CellRendererPixbuf()
        col = gtk.TreeViewColumn(_("Speed"), r, pixbuf = MirrorSelectionDialog.MIRROR_SPEED_BAR_COLUMN)
        self._treeview.append_column(col)
        col.set_sort_column_id(MirrorSelectionDialog.MIRROR_SPEED_COLUMN)
        col.set_min_width(int(1.1 * SPEED_PIX_WIDTH))
        
        self._speed_test_lock = thread.allocate_lock()
        self._current_speed_test_index = -1
        self._best_speed = -1
        
        self._speed_pixbufs = {}
        self.country_info = CountryInformation()
    
    def _update_list(self):
        self._mirrors_model.clear()
        for mirror in self._mirrors:
            flag = "/usr/lib/linuxmint/mintSources/flags/generic.png"
            if os.path.exists("/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()):
                flag = "/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()            
            self._mirrors_model.append((
                mirror,
                mirror.url,
                gtk.gdk.pixbuf_new_from_file(flag),
                -1,
                None
            ))
        self._next_speed_test()
    
    def _next_speed_test(self):
        test_mirror = None
        for i in range(len(self._mirrors_model)):
            url = self._mirrors_model[i][MirrorSelectionDialog.MIRROR_URL_COLUMN]
            speed = self._mirrors_model[i][MirrorSelectionDialog.MIRROR_SPEED_COLUMN]
            if speed == -1:
                test_mirror = url
                self._current_speed_test_index = i
                break
        if test_mirror:
            self._speed_test_result = None
            gobject.timeout_add(100, self._check_speed_test_done)
            thread.start_new_thread(self._speed_test, (test_mirror,))
    
    def _check_speed_test_done(self):
        self._speed_test_lock.acquire()
        speed_test_result = self._speed_test_result
        self._speed_test_lock.release()
        if speed_test_result != None and len(self._mirrors_model) > 0:
            self._mirrors_model[self._current_speed_test_index][MirrorSelectionDialog.MIRROR_SPEED_COLUMN] = speed_test_result
            self._best_speed = max(self._best_speed, speed_test_result)
            self._update_relative_speeds()
            self._next_speed_test()
            return False
        else:
            return True
    
    def _update_relative_speeds(self):
        if self._best_speed > 0:
            for i in range(len(self._mirrors_model)):
                self._mirrors_model[i][MirrorSelectionDialog.MIRROR_SPEED_BAR_COLUMN] = self._get_speed_pixbuf(int(100 * self._mirrors_model[i][MirrorSelectionDialog.MIRROR_SPEED_COLUMN] / self._best_speed))
    
    def _get_speed_pixbuf(self, speed):
        represented_speed = 10 * (speed / 10)
        if speed > 0:
            if not speed in self._speed_pixbufs:
                color_pix = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, False, 8, SPEED_PIX_WIDTH * speed / 100, SPEED_PIX_HEIGHT)
                red = 0xff000000
                green = 0x00ff0000
                if represented_speed > 50:
                    red_level = (100 - represented_speed) / 50.
                    green_level = 1
                else:
                    red_level = 1
                    green_level = (represented_speed / 50.)
                red_level = int(255 * red_level) * 0x01000000
                green_level = int(255 * green_level) * 0x00010000
                color = red_level + green_level
                color_pix.fill(color)
                final_pix = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, False, 8, SPEED_PIX_WIDTH, SPEED_PIX_HEIGHT)
                final_pix.fill(0xffffffff)
                color_pix.copy_area(0, 0, SPEED_PIX_WIDTH * speed / 100, SPEED_PIX_HEIGHT, final_pix, 0, 0)
                del color_pix
                self._speed_pixbufs[speed] = final_pix
            pix = self._speed_pixbufs[speed]
        else:
            pix = None
        return pix
    
    def _speed_test(self, url):
        try:
            c = pycurl.Curl()
            buff = cStringIO.StringIO()
            c.setopt(pycurl.URL, url)
            c.setopt(pycurl.CONNECTTIMEOUT, 10)
            c.setopt(pycurl.TIMEOUT, 10)
            c.setopt(pycurl.FOLLOWLOCATION, 1)
            c.setopt(pycurl.WRITEFUNCTION, buff.write)
            c.perform()
            download_speed = c.getinfo(pycurl.SPEED_DOWNLOAD)
        except:
            download_speed = -2
        self._speed_test_lock.acquire()
        self._speed_test_result = download_speed
        self._speed_test_lock.release()
    
    def run(self, mirrors):
        self._mirrors = mirrors
        self._best_speed = -1
        self._update_list()
        self._dialog.show_all()
        if self._dialog.run() == gtk.RESPONSE_APPLY:
            try:
                model, path = self._treeview.get_selection().get_selected_rows()
                iter = model.get_iter(path[0])
                res = model.get(iter, MirrorSelectionDialog.MIRROR_URL_COLUMN)[0]
            except:
                res = None
        else:
            res = None
        self._dialog.hide()
        self._mirrors_model.clear()
        self._mirrors = None
        return res

class Application(object):
    def __init__(self):

        self.lsb_codename = commands.getoutput("lsb_release -sc")
        print "Using codename: %s" % self.lsb_codename

        glade_file = "/usr/lib/linuxmint/mintSources/mintSources.glade"
            
        self.builder = gtk.Builder()
        self.builder.add_from_file(glade_file)
        self._main_window = self.builder.get_object("main_window")
        self._notebook = self.builder.get_object("notebook")
        self._official_repositories_box = self.builder.get_object("official_repositories_box")        
               
        config_parser = ConfigParser.RawConfigParser()
        config_parser.read("/usr/share/mintsources/%s/mintsources.conf" % self.lsb_codename)
        self.config = {}
        self.optional_components = []
        for section in config_parser.sections():
            if section.startswith("optional_component"):
                component = Component(config_parser.get(section, "name"), config_parser.get(section, "description"), False)
                self.optional_components.append(component)
            else:
                self.config[section] = {}                        
                for param in config_parser.options(section):                
                    self.config[section][param] = config_parser.get(section, param)          

        self.builder.get_object("label_mirrors").set_markup("<b>%s</b>" % _("Mirrors"))    
        self.builder.get_object("label_mirror_description").set_markup("%s (%s)" % (_("Main"), self.config["general"]["codename"]) )
        self.builder.get_object("label_base_mirror_description").set_markup("%s (%s)" % (_("Base"), self.config["general"]["base_codename"]) )
        self.builder.get_object("button_mirror").set_tooltip_text("Select server...")
        self.builder.get_object("button_base_mirror").set_tooltip_text("Select server...")

        self.builder.get_object("label_optional_components").set_markup("<b>%s</b>" % _("Optional components"))                    
        self.builder.get_object("label_source_code").set_markup("<b>%s</b>" % _("Source code"))
        
        self.builder.get_object("label_description").set_markup("<b>%s</b>" % self.config["general"]["description"])
        self.builder.get_object("image_icon").set_from_file("/usr/share/mintsources/%s/icon.png" % self.lsb_codename)
               
        self.selected_components = []
        if (len(self.optional_components) > 0):            
            components_table = gtk.Table()
            self.builder.get_object("vbox_optional_components").pack_start(components_table, True, True)
            self.builder.get_object("vbox_optional_components").show_all()
            nb_components = 0
            for i in range(len(self.optional_components)):
                component = self.optional_components[i]                
                cb = ComponentToggleCheckBox(self, component)
                component.set_widget(cb)
                components_table.attach(cb, 0, 1, nb_components, nb_components + 1, xoptions = gtk.FILL | gtk.EXPAND, yoptions = 0)
                nb_components += 1   


        self.mirrors = []
        mirrorsfile = open(self.config["mirrors"]["mirrors"], "r")
        for line in mirrorsfile.readlines():
            line = line.strip()
            if ("#LOC:" in line):
                country_code = line.split(":")[1]
            else:
                if country_code is not None:
                    mirror = Mirror(line, country_code)
                    self.mirrors.append(mirror)

        self.base_mirrors = []
        mirrorsfile = open(self.config["mirrors"]["base_mirrors"], "r")
        for line in mirrorsfile.readlines():
            line = line.strip()
            if ("#LOC:" in line):
                country_code = line.split(":")[1]
            else:
                if country_code is not None:
                    mirror = Mirror(line, country_code)
                    self.base_mirrors.append(mirror)        

        self.detect_official_sources()     

        self.builder.get_object("revert_button").connect("clicked", self.revert_to_default_sources)
        
        self.builder.get_object("apply_button").connect("clicked", self.apply_official_sources)
        
        self._tab_buttons = [
            self.builder.get_object("toggle_official_repos"),
            self.builder.get_object("toggle_ppas"),
            self.builder.get_object("toggle_additional_repos"),
            self.builder.get_object("toggle_authentication_keys")
        ]
        
        self._main_window.connect("delete_event", lambda w,e: gtk.main_quit())
        for i in range(len(self._tab_buttons)):
            self._tab_buttons[i].connect("clicked", self._on_tab_button_clicked, i)
            self._tab_buttons[i].set_active(False)
                
        self.builder.get_object("menu_item_close").connect("activate", lambda w: gtk.main_quit())
        
        self.mirror_selection_dialog = MirrorSelectionDialog(self, self.builder)

        self.builder.get_object("button_mirror").connect("clicked", self.select_new_mirror)
        self.builder.get_object("button_base_mirror").connect("clicked", self.select_new_base_mirror)

    def select_new_mirror(self, widget):
        url = self.mirror_selection_dialog.run(self.mirrors)
        if url is not None:
            self.selected_mirror = url
            self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.update_flags()

    def select_new_base_mirror(self, widget):
        url = self.mirror_selection_dialog.run(self.base_mirrors)
        if url is not None:
            self.selected_base_mirror = url
            self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror)
        self.update_flags()

    def _on_tab_button_clicked(self, button, page_index):
        if page_index == self._notebook.get_current_page() and button.get_active() == True:
            return
        if page_index != self._notebook.get_current_page() and button.get_active() == False:
            return
        self._notebook.set_current_page(page_index)
        for i in self._tab_buttons:
            i.set_active(False)
        button.set_active(True)
    
    def run(self):
        gobject.threads_init()
        self._main_window.show_all()
        gtk.main()

    def revert_to_default_sources(self, widget):
        self.selected_mirror = self.config["mirrors"]["default"]
        self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.selected_base_mirror = self.config["mirrors"]["base_default"]
        self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror)
        self.update_flags()

        self.builder.get_object("source_code_cb").set_active(False)

        for component in self.optional_components:
            component.selected = False
            component.widget.set_active(False)

        self.apply_official_sources()


    def apply_official_sources(self, widget=None):

        # Check which components are selected
        selected_components = []        
        for component in self.optional_components:
            if component.selected:
                selected_components.append(component.name)

        # Update official packages repositories
        os.system("rm -f /etc/apt/sources.list.d/official-package-repositories.list")                
        template = open('/usr/share/mintsources/%s/official-package-repositories.list' % self.lsb_codename, 'r').read()
        template = template.replace("$codename", self.config["general"]["codename"])
        template = template.replace("$basecodename", self.config["general"]["base_codename"])
        template = template.replace("$optionalcomponents", ' '.join(selected_components))  
        template = template.replace("$mirror", self.selected_mirror)
        template = template.replace("$basemirror", self.selected_base_mirror)

        with open("/etc/apt/sources.list.d/official-package-repositories.list", "w") as text_file:
            text_file.write(template)

        # Update official sources repositories
        os.system("rm -f /etc/apt/sources.list.d/official-source-repositories.list")
        if (self.builder.get_object("source_code_cb").get_active()):
            template = open('/usr/share/mintsources/%s/official-source-repositories.list' % self.lsb_codename, 'r').read()
            template = template.replace("$codename", self.config["general"]["codename"])
            template = template.replace("$basecodename", self.config["general"]["base_codename"])
            template = template.replace("$optionalcomponents", ' '.join(selected_components))
            template = template.replace("$mirror", self.selected_mirror)
            template = template.replace("$basemirror", self.selected_base_mirror)
            with open("/etc/apt/sources.list.d/official-source-repositories.list", "w") as text_file:
                text_file.write(template)        

    def detect_official_sources(self):
        self.selected_mirror = self.config["mirrors"]["default"]
        self.selected_base_mirror = self.config["mirrors"]["base_default"]

        # Detect source code repositories
        self.builder.get_object("source_code_cb").set_active(os.path.exists("/etc/apt/sources.list.d/official-source-repositories.list"))

        listfile = open('/etc/apt/sources.list.d/official-package-repositories.list', 'r')
        for line in listfile.readlines():
            if (self.config["detection"]["main_identifier"] in line):
                for component in self.optional_components:
                    if component.name in line:
                        component.widget.set_active(True)
                elements = line.split(" ")
                if elements[0] == "deb":                    
                    mirror = elements[1]                    
                    if "$" not in mirror:
                        self.selected_mirror = mirror
            if (self.config["detection"]["base_identifier"] in line):
                elements = line.split(" ")
                if elements[0] == "deb":                    
                    mirror = elements[1]
                    if "$" not in mirror:
                        self.selected_base_mirror = mirror

        self.builder.get_object("label_mirror_name").set_text(self.selected_mirror)
        self.builder.get_object("label_base_mirror_name").set_text(self.selected_base_mirror) 

        self.update_flags()
    
    def update_flags(self):
        self.builder.get_object("image_mirror").set_from_file("/usr/lib/linuxmint/mintSources/flags/generic.png") 
        self.builder.get_object("image_base_mirror").set_from_file("/usr/lib/linuxmint/mintSources/flags/generic.png") 

        selected_mirror = self.selected_mirror
        if selected_mirror[-1] == "/":
            selected_mirror = selected_mirror[:-1]

        selected_base_mirror = self.selected_base_mirror
        if selected_base_mirror[-1] == "/":
            selected_base_mirror = selected_base_mirror[:-1]

        for mirror in self.mirrors:
            if mirror.url[-1] == "/":
                url = mirror.url[:-1]
            else:
                url = mirror.url
            if url in selected_mirror:
                if os.path.exists("/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()):
                    self.builder.get_object("image_mirror").set_from_file("/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()) 

        for mirror in self.base_mirrors:
            if mirror.url[-1] == "/":
                url = mirror.url[:-1]
            else:
                url = mirror.url
            if url in selected_base_mirror:
                if os.path.exists("/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()):
                    self.builder.get_object("image_base_mirror").set_from_file("/usr/lib/linuxmint/mintSources/flags/%s.png" % mirror.country_code.lower()) 

if __name__ == "__main__":
    if os.getuid() != 0:
        os.execvp("gksu", ("", " ".join(sys.argv)))
    else:
        Application().run()
